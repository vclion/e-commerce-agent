export const session = {
  get workspace() { return sessionStorage.getItem('commerce-workspace') || 'today' },
  set workspace(value: string) { sessionStorage.setItem('commerce-workspace', value) },
  get token() { return sessionStorage.getItem('commerce-token') || '' },
  set token(value: string) { value ? sessionStorage.setItem('commerce-token', value) : sessionStorage.removeItem('commerce-token') },
  get shopId() { return sessionStorage.getItem('commerce-shop') || '' },
  set shopId(value: string) { value ? sessionStorage.setItem('commerce-shop', value) : sessionStorage.removeItem('commerce-shop') }
}

function headers() {
  return {'Content-Type': 'application/json', Authorization: `Bearer ${session.token}`,
    ...(session.shopId ? {'X-Shop-ID': session.shopId} : {})}
}

export async function rawApi(path: string, body?: File) {
  const response = await fetch(path.startsWith('/api/') ? path : `/api${path}`, {
    method: body ? 'POST' : 'GET',
    headers: {Authorization: `Bearer ${session.token}`, ...(session.shopId ? {'X-Shop-ID': session.shopId} : {})},
    body
  })
  if (!response.ok) {
    const error = await response.json()
    throw new Error(typeof error.detail === 'string' ? error.detail : '文件请求失败')
  }
  return response
}

export async function api(path: string, method = 'GET', body?: unknown) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: headers(),
    body: body === undefined ? undefined : JSON.stringify(body)
  })
  const data = await response.json()
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((x: {msg: string}) => x.msg).join('；') : data.detail
    throw new Error(detail || `请求失败 (${response.status})`)
  }
  return data
}

export async function chatStream(query: string, conversationId: string, receive: (event: string, data: any) => void, workspace = 'general', sessionId = '', temporary = false, clientRequestId = '', retryOf = '') {
  const response = await fetch('/api/chat', {
    method: 'POST', headers: headers(),
    body: JSON.stringify({query, conversation_id: conversationId, workspace, session_id: sessionId, temporary,
      client_request_id: clientRequestId, retry_of: retryOf})
  })
  if (!response.ok) { const data = await response.json(); throw new Error(data.detail || '对话请求失败') }
  if (!response.body) throw new Error('浏览器未提供流式响应')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = '', completed = false
  function dispatch(frame: string) {
    const lines = frame.split('\n')
    const event = lines.find(line => line.startsWith('event:'))?.slice(6).trim() || 'message'
    const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n')
    if (data) {
      if (event === 'done') completed = true
      receive(event, JSON.parse(data))
    }
  }
  try {
    while (true) {
      const {value, done} = await reader.read()
      buffer = (buffer + decoder.decode(value || new Uint8Array(), {stream: !done})).replace(/\r\n/g, '\n')
      let separator: number
      while ((separator = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, separator); buffer = buffer.slice(separator + 2)
        dispatch(frame)
      }
      if (done) break
    }
    if (buffer.trim()) dispatch(buffer)
    if (!completed) throw new Error('回复连接中断，尚未收到完整结果，请重试这条消息。')
  } finally { reader.releaseLock() }
}
