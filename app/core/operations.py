from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import Operation
PREFIXES={"activity":"ACT","deployment":"DEP","change":"CR","incident":"INC","backup":"BKP"}
def create_operation(db:Session,kind:str,**values)->Operation:
    prefix=PREFIXES.get(kind,"OP")
    # The row is allocated in the caller's transaction. PostgreSQL migration can use sequences.
    number=(db.scalar(select(func.count(Operation.id))) or 0)+1
    operation=Operation(public_id=f"{prefix}-{number:06d}",kind=kind,**values); db.add(operation); db.flush(); return operation
