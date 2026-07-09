"""Thin Tenant API client for Gammavet driver availability confirmations."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urljoin
from uuid import UUID

import requests
from chask_foundation.backend.models import OrchestrationEvent

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TENANT_CONFIRM_PATH = "gammavet/drivers/confirm-availability"
ACTOR_LAMBDA = "gammavet_confirm_availability"
DEFAULT_TENANT_SLUG = "chask"
PROD_GAMMAVET_ORG_UUID = "7a95d94b-6f55-4eb4-b971-47f77cb29e46"
PROD_GAMMAVET_TENANT_SLUG = "gammavet"
DEFAULT_TENANT_BRANCH = "test"
DEFAULT_TIMEOUT = 30
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)

AFFIRMATIVE_WORDS = (
    "confirmar",
    "confirmo",
    "confirmado",
    "si",
    "ok",
    "listo",
    "disponible",
)
NEGATIVE_WORDS = (
    "rechazar",
    "rechazo",
    "rechazado",
    "no",
    "no puedo",
    "indisponible",
    "ocupado",
)


class FunctionBackend:
    """Confirm a driver as available today when the WhatsApp reply is affirmative."""

    def __init__(self, orchestration_event: OrchestrationEvent):
        self.orchestration_event = orchestration_event
        logger.info(
            "Initialized GammavetConfirmAvailabilityFn for org=%s",
            orchestration_event.organization.organization_id,
        )

    def process_request(self) -> str:
        decision = self._decision()
        driver_phone = self._driver_phone()

        if decision == "decline":
            return json.dumps(
                {
                    "confirmed": False,
                    "declined": True,
                    "driver_phone": driver_phone,
                    "message": "Conductor rechazo disponibilidad; queda sin confirmar.",
                },
                ensure_ascii=False,
            )
        if decision != "confirm":
            raise ValueError(
                "No se pudo clasificar la respuesta. Esperado: Confirmar o Rechazar."
            )
        if not driver_phone:
            raise ValueError("No se encontro driver_phone/user_phone_number para el conductor")

        payload = {
            "driver_phone": driver_phone,
            "orchestration_event_uuid": str(self._event_uuid()),
            "actor_lambda": ACTOR_LAMBDA,
        }
        result = self._post_tenant_api(TENANT_CONFIRM_PATH, payload)
        return json.dumps(
            {
                "confirmed": True,
                "declined": False,
                "driver_phone": driver_phone,
                "tenant_result": result,
            },
            ensure_ascii=False,
        )

    def _decision(self) -> str:
        args = self._extract_tool_args()
        candidates = [
            args.get("action"),
            args.get("respuesta"),
            args.get("response"),
            args.get("button_text"),
            args.get("button_payload"),
            args.get("message"),
            args.get("texto"),
            (self.orchestration_event.extra_params or {}).get("button_text"),
            (self.orchestration_event.extra_params or {}).get("button_payload"),
            getattr(self.orchestration_event, "prompt", None),
        ]
        text = " ".join(str(value or "") for value in candidates).strip().casefold()
        if any(word in text for word in NEGATIVE_WORDS):
            return "decline"
        if any(word in text for word in AFFIRMATIVE_WORDS):
            return "confirm"
        return ""

    def _driver_phone(self) -> str | None:
        args = self._extract_tool_args()
        extra_params = self.orchestration_event.extra_params or {}
        for value in (
            args.get("driver_phone"),
            args.get("telefono_conductor"),
            args.get("telefono"),
            args.get("phone"),
            extra_params.get("driver_phone"),
            extra_params.get("user_phone_number"),
            extra_params.get("from_phone"),
            extra_params.get("phone"),
        ):
            if value:
                return str(value).strip()
        return None

    def _event_uuid(self) -> UUID:
        raw_event_id = getattr(self.orchestration_event, "event_id", None)
        try:
            return UUID(str(raw_event_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid orchestration_event.event_id: expected UUID, "
                f"got {raw_event_id!r}"
            ) from exc

    def _extract_tool_args(self) -> dict[str, Any]:
        extra_params = self.orchestration_event.extra_params or {}
        tool_calls = extra_params.get("tool_calls", [])
        if not tool_calls:
            return {}
        raw_args = tool_calls[0].get("args", {}) or {}
        if isinstance(raw_args, str):
            try:
                parsed_args = json.loads(raw_args)
            except json.JSONDecodeError:
                return {}
            return parsed_args if isinstance(parsed_args, dict) else {}
        return raw_args if isinstance(raw_args, dict) else {}

    def _post_tenant_api(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        session = requests.Session()
        token = self._exchange_tenant_token(session)
        url = urljoin(self._tenant_base_url(), path.lstrip("/"))
        response = self._request_with_retries(
            session,
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        data = self._response_json_or_raise(response)
        if not isinstance(data, dict):
            raise ValueError(f"Tenant API {path} returned a non-object response")
        return data

    def _exchange_tenant_token(self, session: requests.Session) -> str:
        env_jwt = os.environ.get("TENANT_JWT")
        if env_jwt:
            return env_jwt

        response = self._request_with_retries(
            session,
            "POST",
            f"{self._control_plane_base_url()}/auth/exchange-tenant-token",
            headers=self._control_plane_headers(),
            json={
                "org_uuid": self.orchestration_event.organization.organization_id,
                "branch": self._tenant_branch(),
            },
        )
        data = self._response_json_or_raise(response)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ValueError("Tenant token response missing access_token")
        return str(token)

    def _tenant_base_url(self) -> str:
        override_url = os.getenv("CHASK_TENANT_API_BASE_URL")
        if override_url:
            return override_url.rstrip("/") + "/"
        if self._tenant_branch() == "prod":
            return f"https://{self._tenant_slug()}.chask.co/api/"
        return f"https://{self._tenant_slug()}.chask.co/api/test/"

    def _control_plane_base_url(self) -> str:
        explicit_base_url = os.getenv("CHASK_API_BASE_URL")
        if explicit_base_url:
            return explicit_base_url.rstrip("/")
        base_domain = os.getenv("BASE_DOMAIN")
        if base_domain:
            return f"https://{base_domain}/api/v2"
        mode = os.getenv("MODE", os.getenv("GLOBAL_SERVER", "DEVELOPMENT")).upper()
        if mode == "PRODUCTION":
            return "https://app.chask.io/api/v2"
        return "https://app.chask.it/api/v2"

    def _control_plane_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Organization-ID": self.orchestration_event.organization.organization_id,
        }
        access_token = getattr(self.orchestration_event, "access_token", None)
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _tenant_branch(self) -> str:
        branch = os.environ.get("TENANT_BRANCH") or os.environ.get("CHASK_TENANT_BRANCH")
        if not branch:
            base_domain = os.getenv("BASE_DOMAIN", "")
            if base_domain == "app.chask.io":
                branch = "prod"
            else:
                branch = getattr(self.orchestration_event, "branch", None) or DEFAULT_TENANT_BRANCH
        if branch not in ("prod", "test"):
            raise ValueError(f"Invalid branch: {branch}")
        return branch

    def _tenant_slug(self) -> str:
        tenant_slug = os.environ.get("TENANT_SLUG") or os.environ.get("CHASK_TENANT_SLUG")
        if tenant_slug:
            return tenant_slug
        org_uuid = self.orchestration_event.organization.organization_id
        if self._is_prod_control_plane() and org_uuid == PROD_GAMMAVET_ORG_UUID:
            return PROD_GAMMAVET_TENANT_SLUG
        return DEFAULT_TENANT_SLUG

    def _is_prod_control_plane(self) -> bool:
        if os.getenv("BASE_DOMAIN") == "app.chask.io":
            return True
        return os.getenv("MODE", os.getenv("GLOBAL_SERVER", "")).upper() == "PRODUCTION"

    def _request_with_retries(
        self,
        session: requests.Session,
        method: str,
        url: str,
        **request_kwargs: Any,
    ) -> requests.Response:
        last_error: requests.RequestException | None = None
        for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
            try:
                response = session.request(method, url, timeout=DEFAULT_TIMEOUT, **request_kwargs)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < len(RETRY_BACKOFF_SECONDS):
                    time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                    continue
                raise
            if response.status_code >= 500 and attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            return response
        if last_error is not None:
            raise last_error
        raise RuntimeError("Tenant API request exhausted retries without a response")

    def _response_json_or_raise(self, response: requests.Response) -> dict[str, Any] | list[Any]:
        try:
            data = response.json()
        except ValueError:
            data = {"detail": response.text}
        if 200 <= response.status_code < 300:
            return data
        detail = data.get("detail") if isinstance(data, dict) else data
        raise requests.HTTPError(
            f"HTTP {response.status_code} from {response.url}: {detail}",
            response=response,
        )
