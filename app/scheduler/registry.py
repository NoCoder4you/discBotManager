import re
from app.adapters.registry import get_adapter
from app.scheduler.types import RegisteredTask

SAFE_TASK_ID=re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")

class TaskRegistry:
    """Resolves only trusted task objects exposed by registered adapters."""
    def tasks_for(self,bot)->tuple[RegisteredTask,...]:
        tasks=get_adapter(bot.adapter).get_tasks(); seen=set()
        for task in tasks:
            if not SAFE_TASK_ID.fullmatch(task.id): raise ValueError(f"Unsafe registered task ID: {task.id}")
            if task.id in seen: raise ValueError(f"Duplicate registered task: {task.id}")
            if not 1 <= task.timeout_seconds <= 86400: raise ValueError("Task timeout is outside safe limits")
            seen.add(task.id)
        return tuple(tasks)
    def resolve(self,bot,task_id:str)->RegisteredTask|None:
        if not SAFE_TASK_ID.fullmatch(task_id): return None
        return next((task for task in self.tasks_for(bot) if task.id==task_id),None)
