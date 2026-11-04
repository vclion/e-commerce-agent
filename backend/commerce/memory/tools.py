import re
from fastapi import HTTPException
from ..models import ChatRun
from ..config import settings
from . import service, retrieval
from .schemas import MemoryCreate, MemorySearch, Forget, HistoryRead
from .policy import preferences
from .models import MemoryRunContext, ChatSession

READ_OPERATIONS={'memory_search','memory_get','history_read'}
WRITE_OPERATIONS={'memory_propose','memory_forget'}


def execute(db, actor, token, op, args):
    run=db.get(ChatRun,token.request_id)
    prefs=preferences(db,actor.id)
    snapshot=db.get(MemoryRunContext,token.request_id)
    if snapshot and snapshot.timings.get('temporary'):
        raise HTTPException(403,'临时对话不读取或形成记忆，请使用记忆管理处理已有记忆')
    if op == 'history_read':
        if not run or run.user_id != actor.id or not snapshot or not run.session_id:
            raise HTTPException(403, '原文回查需要当前聊天上下文')
        body = HistoryRead.model_validate(args)
        session = db.get(ChatSession, run.session_id)
        from .indexes import read_history
        return read_history(db, session, run, snapshot, body)
    if op in READ_OPERATIONS and (not settings.memory_enabled or not prefs.read_enabled):
        return {'items':[],'status':'disabled'}
    if op=='memory_propose' and not settings.memory_enabled:
        raise HTTPException(403,'记忆功能已停用')
    if op=='memory_search':
        body=MemorySearch.model_validate({**args,'workspace':token.workspace})
        return retrieval.search(db,actor,body.query,body.workspace,body.limit)
    if op=='memory_get':
        row=service.get_item(db,actor,str(args.get('id','')),token.workspace)
        return service.item_view(db,actor,row)
    if not run or run.user_id!=actor.id:
        raise HTTPException(403,'记忆写入需要当前聊天请求')
    if op=='memory_forget':
        if not re.search(r'忘记|删除.{0,12}记忆|不要再记|不再使用.{0,12}记忆',run.query):
            raise HTTPException(403,'需要用户明确要求遗忘')
        return service.forget(db,actor,Forget.model_validate(args).item_ids)
    quote=str(args.pop('quote',''))
    if not quote or quote not in run.query:
        raise HTTPException(422,'请提供当前用户消息中的原文依据')
    explicit=bool(re.search(r'记住|以后|今后|默认|偏好',run.query))
    save_requested=bool(re.search(r'记住|记下来|(?:保存|记录).{0,12}(?:记忆|偏好|约定)',run.query))
    save_denied=bool(re.search(r'(?:不要|别|不用|无需|不必)(?:再)?(?:记住|保存|记录|记下来)',run.query))
    if not prefs.auto_write and (not save_requested or save_denied):
        return {'status':'disabled', 'notice':'长期记忆自动写入已关闭；需要保存时请明确要求记住或使用记忆管理'}
    explicit=explicit or save_requested
    body=MemoryCreate.model_validate({**args,'workspace':token.workspace})
    if body.scope=='personal' and actor.shop_id:
        raise HTTPException(422,'店铺对话的记忆请保存在店铺或当前栏目')
    row=service.create(db,actor,body,source={'type':'chat','id':run.id,'role':'user','quote':quote},
                       candidate=not explicit,explicit=explicit)
    return service.item_view(db,actor,row) if row else {'status':'unchanged'}
