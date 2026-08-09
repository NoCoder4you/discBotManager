from app.adapters.base import BaseBotAdapter

_adapters: dict[str, BaseBotAdapter] = {"base": BaseBotAdapter(), "python": BaseBotAdapter()}

def register_adapter(key: str, adapter: BaseBotAdapter) -> None:
    if not key or key in _adapters: raise ValueError(f"Duplicate adapter: {key}")
    _adapters[key] = adapter

def get_adapter(key: str) -> BaseBotAdapter:
    return _adapters.get(key, _adapters["base"])
