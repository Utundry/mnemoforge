from fastapi import APIRouter
from pydantic import BaseModel
from app.dependencies import KnowledgeTreeDep

router = APIRouter(tags=["knowledge_tree"])

class TreeSliceRequest(BaseModel):
    query: str
    agent_id: str = "default"
    limit: int = 20

@router.post("/knowledge-tree/slice")
async def tree_slice(
    req: TreeSliceRequest,
    tree: KnowledgeTreeDep,
):
    """
    Возвращает срез дерева знаний для запроса, 
    используя гибридное ранжирование (семантика + иерархия).
    """
    return await tree.slice_tree(req.query, req.agent_id, req.limit)