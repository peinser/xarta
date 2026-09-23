from __future__ import annotations

import asyncio
import base64
import hashlib
import time

from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import quote

import aiohttp
import orjson

from lxml import etree  # type: ignore[import-untyped]

from xarta.services.v1.doccle.configuration import ADAPTER_NAME
from xarta.services.v1.doccle.configuration import validate_doccle_configuration
from xarta.services.v1.doccle.models import DoccleAdapter
from xarta.services.v1.doccle.models import DoccleDocument
from xarta.services.v1.doccle.models import DoccleReceiverProfile
from xarta.services.v1.doccle.models import DoccleResult
from xarta.services.v1.doccle.models import DoccleResultCategory
from xarta.services.v1.doccle.models import DoccleTransportCategory

if TYPE_CHECKING:
    from collections.abc import Mapping


REST_NAMESPACE = "http://www.atosworldline.com/archivingPortal/rest"
RECEIVER_NAMESPACE = "http://www.atosworldline.com/archivingPortal/receivers"
DOCUMENT_NAMESPACE = "http://www.atosworldline.com/archivingPortal/documents"
XML_NAMESPACES = {
    "rest": REST_NAMESPACE,
    "rcv": RECEIVER_NAMESPACE,
    "doc": DOCUMENT_NAMESPACE,
}


class DoccleTransportError(Exception):
    def __init__(self, category: DoccleTransportCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


class DoccleSafePreTransmissionError(DoccleTransportError):
    def __init__(self, message: str) -> None:
        super().__init__(DoccleTransportCategory.SAFE_PRETRANSMISSION, message)


class DoccleAmbiguousTransportError(DoccleTransportError):
    def __init__(self, message: str) -> None:
        super().__init__(DoccleTransportCategory.AMBIGUOUS, message)


class DoccleResponseError(ValueError):
    pass


def _element(namespace: str, name: str, value: str | None = None):
    element = etree.Element(etree.QName(namespace, name))
    if value is not None:
        element.text = value
    return element


def build_doccle_receiver_request(
    receiver_id: str, profile: DoccleReceiverProfile
) -> bytes:
    root = etree.Element(
        etree.QName(REST_NAMESPACE, "createUpdateReceiver"),
        nsmap={None: REST_NAMESPACE, "rcv": RECEIVER_NAMESPACE},
    )
    receiver = _element(REST_NAMESPACE, "receiver")
    receiver.append(_element(RECEIVER_NAMESPACE, "id", receiver_id))
    receiver.append(_element(RECEIVER_NAMESPACE, "action", "Store"))
    labels = _element(RECEIVER_NAMESPACE, "labels")
    labels.append(_element(RECEIVER_NAMESPACE, "label-default", profile.label))
    receiver.append(labels)
    if profile.first_name is not None or profile.last_name is not None:
        personal = _element(RECEIVER_NAMESPACE, "personal-information")
        if profile.first_name is not None:
            personal.append(
                _element(RECEIVER_NAMESPACE, "firstName", profile.first_name)
            )
        if profile.last_name is not None:
            personal.append(_element(RECEIVER_NAMESPACE, "lastName", profile.last_name))
        receiver.append(personal)
    if profile.email is not None or profile.language is not None:
        contact = _element(RECEIVER_NAMESPACE, "contact-details")
        if profile.email is not None:
            contact.append(_element(RECEIVER_NAMESPACE, "email", profile.email))
        if profile.language is not None:
            contact.append(
                _element(RECEIVER_NAMESPACE, "languageISO", profile.language)
            )
        receiver.append(contact)
    root.append(receiver)
    return bytes(
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
    )


def build_doccle_document_request(
    receiver_id: str, document: DoccleDocument, provider_document_type: str
) -> bytes:
    root = etree.Element(
        etree.QName(REST_NAMESPACE, "putDocument"),
        nsmap={"rest": REST_NAMESPACE, "doc": DOCUMENT_NAMESPACE},
    )
    provider_document = _element(REST_NAMESPACE, "document")
    provider_document.append(_element(DOCUMENT_NAMESPACE, "id", document.document_id))
    receiver = _element(DOCUMENT_NAMESPACE, "receiver")
    receiver.append(_element(DOCUMENT_NAMESPACE, "receiver-id", receiver_id))
    provider_document.append(receiver)
    provider_document.append(_element(DOCUMENT_NAMESPACE, "action", "Store"))
    provider_document.append(
        _element(DOCUMENT_NAMESPACE, "sender-document-type", provider_document_type)
    )
    if document.published_at is not None:
        timestamp = document.published_at.isoformat().replace("+00:00", "Z")
        provider_document.append(
            _element(DOCUMENT_NAMESPACE, "creation-datetime", timestamp)
        )
        provider_document.append(
            _element(DOCUMENT_NAMESPACE, "publish-datetime", timestamp)
        )
    provider_document.append(
        _element(DOCUMENT_NAMESPACE, "classification-level", "Public")
    )
    if document.names:
        display = _element(DOCUMENT_NAMESPACE, "document-display")
        display.append(_element(DOCUMENT_NAMESPACE, "presentation-type", "INFO"))
        names = _element(DOCUMENT_NAMESPACE, "name")
        first_language = sorted(document.names)[0]
        for language in sorted(document.names):
            entry = _element(DOCUMENT_NAMESPACE, "entry", document.names[language])
            entry.set("lang", language)
            entry.set("defaultLang", str(language == first_language).lower())
            names.append(entry)
        display.append(names)
        provider_document.append(display)
    document_file = _element(DOCUMENT_NAMESPACE, "document-file")
    document_file.append(_element(DOCUMENT_NAMESPACE, "reference", document.filename))
    file_format = _element(DOCUMENT_NAMESPACE, "format")
    file_format.set("mime-type", document.content_type)
    file_format.set("attribute", "")
    document_file.append(file_format)
    digest = _element(
        DOCUMENT_NAMESPACE,
        "digest",
        base64.b64encode(hashlib.sha256(document.content).digest()).decode("ascii"),
    )
    digest.set("digestAlgorithm", "SHA-256")
    document_file.append(digest)
    document_file.append(
        _element(DOCUMENT_NAMESPACE, "size", str(len(document.content)))
    )
    provider_document.append(document_file)
    root.append(provider_document)
    root.append(
        _element(
            REST_NAMESPACE,
            "newDocumentVersion",
            base64.b64encode(document.content).decode("ascii"),
        )
    )
    return bytes(
        etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
    )


# Public aliases retained for callers; both now generate the official wire schema.
build_receiver_request = build_doccle_receiver_request
build_document_request = build_doccle_document_request


def _parse_xml(body: bytes, *, max_response_bytes: int):
    if len(body) > max_response_bytes:
        raise DoccleResponseError("Doccle response exceeds the configured size limit")
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise DoccleResponseError("Doccle response must not contain DTDs or entities")
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
    )
    try:
        return etree.fromstring(body, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as ex:
        raise DoccleResponseError("Doccle response is not valid XML") from ex


def parse_document_response(body: bytes, *, max_response_bytes: int) -> str:
    root = _parse_xml(body, max_response_bytes=max_response_bytes)
    if etree.QName(root).localname != "documentUri":
        raise DoccleResponseError("Doccle response has an unexpected envelope")
    uri = next(
        (
            child.text.strip()
            for child in root
            if etree.QName(child).localname == "uri"
            and child.text
            and child.text.strip()
        ),
        None,
    )
    if uri is None:
        raise DoccleResponseError("Doccle response does not contain a document URI")
    return str(uri)


def parse_error_response(
    body: bytes, *, max_response_bytes: int
) -> tuple[str | None, str | None]:
    if not body:
        return None, None
    try:
        root = _parse_xml(body, max_response_bytes=max_response_bytes)
    except DoccleResponseError:
        return None, None
    if etree.QName(root).localname != "error":
        return None, None
    values = {etree.QName(child).localname: child.text for child in root}
    return values.get("code"), values.get("message")


def classify_doccle_response(
    status: int,
    body: bytes,
    *,
    max_response_bytes: int,
    operation: str,
) -> DoccleResult:
    if status in {401, 403}:
        return DoccleResult(DoccleResultCategory.AUTHENTICATION_FAILURE)
    if 200 <= status < 300:
        if operation == "receiver":
            return DoccleResult(DoccleResultCategory.PROVISIONED)
        try:
            reference = parse_document_response(
                body, max_response_bytes=max_response_bytes
            )
        except DoccleResponseError:
            return DoccleResult(DoccleResultCategory.PROTOCOL_ERROR)
        return DoccleResult(DoccleResultCategory.STORED, reference)
    code, _ = parse_error_response(body, max_response_bytes=max_response_bytes)
    if status == 404:
        category = DoccleResultCategory.RECEIVER_NOT_FOUND
    elif status in {400, 409}:
        category = DoccleResultCategory.BUSINESS_REJECTION
    else:
        category = DoccleResultCategory.PROTOCOL_ERROR
    return DoccleResult(category, provider_status=code)


class DoccleSenderRESTAdapter:
    name = ADAPTER_NAME

    def __init__(
        self, configuration: Mapping[str, Any], session: aiohttp.ClientSession
    ) -> None:
        validate_doccle_configuration(configuration)
        self._session = session
        self._sender_name = str(configuration["sender_name"])
        self._endpoint = str(configuration["endpoint"]).rstrip("/")
        self._receiver_path = str(
            configuration.get(
                "receiver_path", "/senders/{sender_name}/receivers/{receiver_id}"
            )
        )
        self._document_path = str(
            configuration.get(
                "document_path",
                "/senders/{sender_name}/receivers/{receiver_id}/documents/{document_id}",
            )
        )
        self._timeout = float(configuration["timeout"])
        credentials = configuration["credentials"]
        self._token_url = str(credentials["token_url"])
        self._client_id = str(credentials["client_id"])
        self._client_secret = str(credentials["client_secret"])
        self._scope = credentials.get("scope")
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._document_types = dict(configuration["document_types"])
        self._max_response_bytes = int(
            configuration.get("max_response_bytes", 1_000_000)
        )

    async def create_or_update_receiver(
        self, *, receiver_id: str, profile: DoccleReceiverProfile
    ) -> DoccleResult:
        return await self._request(
            "put",
            self._url(self._receiver_path, receiver_id=receiver_id),
            build_doccle_receiver_request(receiver_id, profile),
            "receiver",
        )

    async def put_document(
        self, *, receiver_id: str, document: DoccleDocument
    ) -> DoccleResult:
        provider_type = self._document_types.get(document.document_type)
        if provider_type is None:
            raise DoccleSafePreTransmissionError(
                "Document type is not configured for the Doccle destination"
            )
        return await self._request(
            "post",
            self._url(
                self._document_path,
                receiver_id=receiver_id,
                document_id=document.document_id,
            ),
            build_doccle_document_request(receiver_id, document, provider_type),
            "document",
        )

    def _url(
        self, path: str, *, receiver_id: str, document_id: str | None = None
    ) -> str:
        rendered = path.format(
            sender_name=quote(self._sender_name, safe=""),
            receiver_id=quote(receiver_id, safe=""),
            document_id=quote(document_id, safe="") if document_id is not None else "",
        )
        return f"{self._endpoint}/{rendered.lstrip('/')}"

    async def _request(
        self, method: str, url: str, payload: bytes, operation: str
    ) -> DoccleResult:
        access_token = await self._get_access_token()
        try:
            request = getattr(self._session, method)(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/xml; charset=utf-8",
                    "Accept": "application/xml",
                },
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                allow_redirects=False,
                ssl=True,
            )
        except (aiohttp.InvalidURL, ValueError) as ex:
            raise DoccleSafePreTransmissionError(
                "Doccle request could not be prepared"
            ) from ex
        except aiohttp.ClientConnectorError as ex:
            raise DoccleSafePreTransmissionError(
                "Doccle endpoint connection failed before transmission"
            ) from ex
        except (TimeoutError, aiohttp.ClientError) as ex:
            raise DoccleAmbiguousTransportError(
                "Doccle request outcome is unknown"
            ) from ex
        try:
            async with request as response:
                body = await _bounded_response(response, self._max_response_bytes)
                return classify_doccle_response(
                    response.status,
                    body,
                    max_response_bytes=self._max_response_bytes,
                    operation=operation,
                )
        except DoccleResponseError:
            return DoccleResult(DoccleResultCategory.PROTOCOL_ERROR)
        except aiohttp.ClientConnectorError as ex:
            raise DoccleSafePreTransmissionError(
                "Doccle endpoint connection failed before transmission"
            ) from ex
        except (TimeoutError, aiohttp.ClientError) as ex:
            raise DoccleAmbiguousTransportError(
                "Doccle request outcome is unknown"
            ) from ex

    async def _get_access_token(self) -> str:
        if (
            self._access_token is not None
            and self._access_token_expires_at > time.monotonic()
        ):
            return self._access_token
        async with self._token_lock:
            if (
                self._access_token is not None
                and self._access_token_expires_at > time.monotonic()
            ):
                return self._access_token
            form = {"grant_type": "client_credentials"}
            if self._scope is not None:
                form["scope"] = str(self._scope)
            try:
                request = self._session.post(
                    self._token_url,
                    data=form,
                    headers={
                        "Accept": "application/json",
                        "Authorization": aiohttp.encode_basic_auth(
                            self._client_id, self._client_secret
                        ),
                    },
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                    allow_redirects=False,
                    ssl=True,
                )
                async with request as response:
                    body = await _bounded_response(response, self._max_response_bytes)
                    if not 200 <= response.status < 300:
                        raise DoccleSafePreTransmissionError(
                            "Doccle OAuth client credentials were rejected"
                        )
            except DoccleSafePreTransmissionError:
                raise
            except (TimeoutError, aiohttp.ClientError) as ex:
                # No Receiver or document request has started while token acquisition
                # is incomplete, so this boundary is safe to retry.
                raise DoccleSafePreTransmissionError(
                    "Doccle OAuth token acquisition failed"
                ) from ex
            try:
                token = orjson.loads(body)
                access_token = token["access_token"]
                expires_in = float(token.get("expires_in", 300))
            except (KeyError, TypeError, ValueError, orjson.JSONDecodeError) as ex:
                raise DoccleSafePreTransmissionError(
                    "Doccle OAuth response is invalid"
                ) from ex
            if not isinstance(access_token, str) or not access_token or expires_in <= 0:
                raise DoccleSafePreTransmissionError(
                    "Doccle OAuth response does not contain a usable token"
                )
            self._access_token = access_token
            self._access_token_expires_at = time.monotonic() + max(
                1, expires_in - min(30, expires_in / 10)
            )
            return access_token


async def _bounded_response(response: aiohttp.ClientResponse, limit: int) -> bytes:
    if response.content_length is not None and response.content_length > limit:
        raise DoccleResponseError("Doccle response exceeds the configured size limit")
    body = bytearray()
    while True:
        chunk = await response.content.read(min(64 * 1024, limit + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > limit:
            raise DoccleResponseError(
                "Doccle response exceeds the configured size limit"
            )


class DoccleSenderRESTAdapterFactory:
    name = ADAPTER_NAME

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    def validate(self, configuration: Mapping[str, Any]) -> None:
        validate_doccle_configuration(configuration)

    def create(self, configuration: Mapping[str, Any]) -> DoccleAdapter:
        return DoccleSenderRESTAdapter(configuration, self._session)
