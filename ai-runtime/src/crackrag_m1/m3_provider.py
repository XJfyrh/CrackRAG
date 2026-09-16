"""DeepSeek native HTTP rendering; admission and settlement stay in Go/Model."""
import asyncio
import json
import httpx

from .m0_snapshot.providers import DeepSeekProvider, ProviderResult
from .m3_prefix import native_json


class NativeDeepSeekProvider(DeepSeekProvider):
    async def complete(self, payload, context):
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                response = await self.client.post('chat/completions',
                    content=native_json(payload).encode('utf-8'))
            try:
                def reject_constant(value):
                    raise ValueError('INVALID_JSON_NUMBER')
                body = json.loads(response.content, parse_constant=reject_constant)
            except (ValueError, UnicodeDecodeError):
                body = None
            allowed = ('x-request-id', 'request-id', 'x-ds-request-id', 'retry-after')
            return ProviderResult(body=body, raw_text=response.text, status_code=response.status_code,
                headers={key: response.headers[key] for key in allowed if key in response.headers})
        except (httpx.TimeoutException, TimeoutError):
            return ProviderResult(transport_failure='TIMEOUT')
        except httpx.RequestError as exc:
            return ProviderResult(transport_failure='TRANSPORT_' + type(exc).__name__)
