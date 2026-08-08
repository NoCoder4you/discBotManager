from dataclasses import dataclass
@dataclass(frozen=True)
class DashboardModule: key:str; name:str; view_permission:str
class ModuleRegistry:
    def __init__(self): self._modules:dict[str,DashboardModule]={}
    def register(self,module:DashboardModule):
        if module.key in self._modules: raise ValueError(f"Duplicate module: {module.key}")
        self._modules[module.key]=module
    def available(self,supported:list[str],allowed): return tuple(m for k,m in self._modules.items() if k in supported and allowed(m.view_permission))
