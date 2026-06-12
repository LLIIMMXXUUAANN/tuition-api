from fastapi.responses import JSONResponse
from pydantic import BaseModel


def _to_camel(s: str) -> str:
    parts = s.split('_')
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def _camel_keys(obj):
    if isinstance(obj, dict):
        return {_to_camel(k): _camel_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camel_keys(i) for i in obj]
    return obj


class CamelResponse(JSONResponse):
    def render(self, content) -> bytes:
        if isinstance(content, BaseModel):
            content = content.model_dump(by_alias=True, mode='json')
        elif isinstance(content, (dict, list)):
            content = _camel_keys(content)
        return super().render(content)
