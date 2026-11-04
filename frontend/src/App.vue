<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ArrowUpRight, ArrowRight, Search as SearchIcon, ShoppingBag, Package, BarChart3, Compass, FileText, LogOut, Sparkles, Send, Plus, RefreshCw, X, Check, ShieldCheck, Clock3, Store, PanelRightClose, PanelRightOpen, ExternalLink, ChevronRight, LoaderCircle, Scale, CircleHelp } from 'lucide-vue-next'
import GarmentArt from './GarmentArt.vue'
import OpsWorkspace from './OpsWorkspace.vue'
import PlatformConnections from './PlatformConnections.vue'
import MemoryManager from './MemoryManager.vue'
import './memory.css'
import { api, chatStream, session } from './api'

type Shop = {id: string; name: string; tagline: string; positioning: string; sizes: string[]; mode?: string}
type User = {id: string; role: string; shop_id: string | null; display_name: string; shop: Shop; shops: Shop[]}
type Product = {shop_id: string; shop_name: string; style_title?: string; available_sizes?: string[]; available_colors?: string[]; style_stock?: number; variants?: Product[]; id: string; title: string; category: string; brand: string; price_fen: number; cost_fen?: number; stock: number; specs: Record<string, string | number>; description: string}
type Message = {role: 'user' | 'assistant'; text: string; shop_id: string | null; error?: boolean; retryQuery?: string; runId?: string; requestId?: string; temporary?: boolean; recovery?: {can_retry: boolean; business_status: string; message: string}; created_at?: string; memory?: {id:string;title:string}[]; sources?: {document_id: string; title: string}[]; effects?: {operation: string; label: string; resource_id?: string}[]}
const user = ref<User | null>(null)
const scopedShops = computed(() => {
  const current = user.value
  const live=current?.shops.filter(s=>(s as any).mode==='live')||[]
  return current?.shop_id ? current.shops.filter(s => s.id === current.shop_id) : live.length?live:current?.shops || []
})
const health = ref<any>(null)
const loading = ref(false)
const toast = ref('')
const toastError = ref(false)
const view = ref(session.workspace)
const mainScroll = ref<HTMLElement | null>(null)
const navigating = ref(false)
const opsViews = ['today','ops-analysis','ops-videos','ops-imports']
const opsRevision = ref(0)
const opsBusy = ref(false)
const loginForm = reactive({username: 'operator', password: ''})
const switchingShop = ref(false)
const products = ref<Product[]>([])
const total = ref(0)
const catalogCategories=ref<string[]>([])
const selected = ref<Product[]>([])
const compareOpen = ref(false)
const orders = ref<any[]>([])
const afterSales = ref<any[]>([])
const drafts = ref<any[]>([])
const stats = ref<any>(null)
const rankings = ref<any>(null)
const competitorRows = ref<any[]>([])
const auditRows = ref<any[]>([])
const filters = reactive({query: '', category: '', maxPrice: '', size: '', fit: '', material: '', offset: 0})
const days = ref(7)
const contentKind = ref('report')
const minMargin = ref(20)
const importOpen = ref(false)
const importText = ref('')
const chatOpen = ref(window.innerWidth >= 980)
const memoryOpen=ref(false), temporaryChat=ref(false)
function memoryChanged(){for(const state of Object.values(chatStates)){state.conversationId='';for(const message of state.messages)message.memory=[]}}
type ChatState = {messages: Message[]; input: string; chatting: boolean; conversationId: string; sessionId:string;
  historyLoading: boolean; historyReady: boolean; historyError: string; hasMore: boolean; offset: number}
const chatStates = reactive<Record<string, ChatState>>({})
const showLegacy = ref(false)
const activeWorkspace = computed(() => showLegacy.value ? 'general' : view.value)
const activeChat = computed(() => {
  const key = `${user.value?.id || ''}:${user.value?.shop_id || 'all'}:${activeWorkspace.value}`
  if (!chatStates[key]) chatStates[key] = {messages: [], input: '', chatting: false, conversationId: '', sessionId:'',
    historyLoading: false, historyReady: false, historyError: '', hasMore: false, offset: 0}
  return chatStates[key]!
})
function chatField<K extends keyof ChatState>(key: K) {
  return computed({get: () => activeChat.value[key], set: (value: ChatState[K]) => {activeChat.value[key] = value}})
}
const messages = chatField('messages'), chatInput = chatField('input'), chatting = chatField('chatting')
const conversationId = chatField('conversationId'), historyLoading = chatField('historyLoading')
const historyReady = chatField('historyReady'), historyError = chatField('historyError')
const messageHasMore = chatField('hasMore'), messageOffset = chatField('offset')
const anyChatting = computed(() => Object.values(chatStates).some(state => state.chatting))
const chatWorkspaceName = computed(() => showLegacy.value ? '旧版总对话' : navigation.find(n => n.id === view.value)?.label || '')
function clearChats() { for (const key of Object.keys(chatStates)) delete chatStates[key]; showLegacy.value = false }
const sourceDocument = ref<any>(null)
const chatScroll = ref<HTMLElement | null>(null)
const shopNames: Record<string, string> = {a: '山序男装', b: '知织女装'}
const chatScopeName = (shopId: string | null | undefined) => shopId
  ? user.value?.shops.find(shop => shop.id === shopId)?.name || shopNames[shopId] || '店铺对话'
  : '总对话'
const categories = computed(() => scopedShops.value.some(s=>s.mode)?catalogCategories.value:user.value?.shop_id !== 'a' ? ['衬衫','T恤','针织衫','卫衣','夹克','风衣','西装','长裤','短裤','连衣裙','半身裙','大衣','羽绒服'] : ['衬衫','T恤','针织衫','卫衣','夹克','风衣','西装','长裤','短裤','大衣','羽绒服'])
const statusNames: Record<string, string> = {delivered: '已签收', shipping: '运输中', shipped:'已发货', paid:'待发货', cancelled:'已取消', completed:'已处理', pending:'待处理', refunded: '已退款', submitted: '已提交', draft: '草稿', published: '已发布', succeeded: '已完成'}
const money = (value: number | null | undefined) => value == null ? '—' : `¥${(value / 100).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
const date = (value: string) => new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12: false})
const aiReady = computed(() => !!health.value?.dify?.operator)
const platformSource = computed(()=>scopedShops.value.some(s=>(s as any).mode==='live')?'正式店铺 · 以同步状态为准':'演示 / 演练数据')
const title = computed(() => ({platform:'连接官方平台数据',today:'今天的运营工作', 'ops-analysis':'全店经营分析', 'ops-videos':'从内容到成片', 'ops-imports':'连接店铺运营数据', catalog: '商品资料', orders: '店铺订单', analytics: '看见数字背后的生意。', research: '下一款好物，从这里发现。', drafts: '让灵感，成为下一步行动。', audit: '每一步，都有迹可循。'}[view.value] || '衣序运营工作台'))
const subtitle = computed(() => ({platform:'保存配置、完成授权并自动同步；无真实凭证也可独立演练。',today:'安排SOP、跟进进度，回填完成证据。', 'ops-analysis':'基于已导入报表核对成交、退款、库存和履约风险。', 'ops-videos':'分析视频表现，拆解同行参考，生成脚本并实际剪辑。', 'ops-imports':'先校验再导入，保留来源与批次；重复文件不会重复计数。', catalog: '查看授权店铺的商品、规格、成本和库存，为内容与选品提供依据。', orders: '查看授权店铺的订单与售后记录，核查履约和退款问题。', analytics: '同一套统计口径，帮助你理解变化、找到方向。', research: '结合需求、毛利与竞品快照，做有依据的选品。', drafts: '管理商品资料、营销内容与经营报告。', audit: '记录业务操作与工具调用，方便回看与排查。'}[view.value] || ''))
const navigation = [
  {id: 'today', label: '今日工作', icon: Clock3},
  {id: 'ops-analysis', label: '全店分析', icon: BarChart3},
  {id: 'ops-videos', label: '短视频运营', icon: Sparkles},
  {id: 'ops-imports', label: '数据导入', icon: Plus},
  {id: 'platform', label: '平台接入', icon: Store},
  {id: 'catalog', label: '商品资料', icon: ShoppingBag},
  {id: 'orders', label: '店铺订单', icon: Package},
  {id: 'research', label: '选品研究', icon: Compass},
  {id: 'drafts', label: '文案与报告', icon: FileText},
  {id: 'audit', label: '运行记录', icon: Clock3}
]
const prompts = computed(() => {
  const byWorkspace: Record<string, string[]> = {
    today: ['读取今天的任务和日报，列出优先事项', '生成今天的SOP，已有任务不要重复创建', '查看每日SOP自动生成计划'],
    'ops-analysis': ['分析最近7天成交、退款与库存风险，标明数据缺口', '比较授权店铺的经营表现', '将本次经营分析保存为报告'],
    'ops-videos': ['查看已有商品和素材，推荐可以制作的短视频', '检查已有脚本的文案事实，列出待核对项', '查看剪辑任务，重试失败的任务'],
    'ops-imports': ['查看最近校验的导入批次，说明哪些可以提交', '查看经营日报的必填字段和示例', '查看可复用的报表字段映射模板'],
    platform: ['查看平台接入状态和缺少的配置', '查询最近的平台同步进度和错误', '对已配置的连接检查状态并同步最近7天数据'],
    catalog: ['筛选当前店铺有库存的通勤衬衫', '比较价格、面料和可售尺码，说明资料缺口', '查看商品事实，说明哪些信息需要补录'],
    orders: ['查看当前店铺订单和售后状态', '查询超过承诺时间尚未发货的订单', '分析退款原因，仅查询和分析，不执行退款'],
    research: ['按现有商品数据推荐选品，说明规则和资料缺口', '比较商品与已有竞品快照', '将本次选品建议保存为报告'],
    drafts: ['查看我保存的营销文案和经营报告', '先查商品事实，再为已有商品生成两版营销文案', '把上一条完整回复保存为草稿'],
    audit: ['查看我最近的工具调用和业务操作记录', '检查最近运行中有哪些失败记录', '解释最近一次操作的实际执行结果'],
  }
  if (byWorkspace[activeWorkspace.value]) return byWorkspace[activeWorkspace.value]!
  if(scopedShops.value.some(s=>s.mode))return ['查询当前店铺已同步数据的覆盖情况，分析最近7天成交与退款', '读取今日SOP与未完成任务，列出优先事项', '先查询本店商品和素材，再帮助我制作短视频脚本']
  const id = user.value?.shop_id === 'b' ? 'WOM-001-01-M' : 'MEN-001-01-M'
  return ['用已导入报表分析最近7天的成交、退款和待处理风险', '读取今日SOP与未完成任务，生成今天的运营重点', `查询${id}的资料与素材，生成并保存三段短视频脚本`]
})
const chartPoints = computed(() => {
  const data = stats.value?.trend || []
  const max = Math.max(...data.map((x: any) => x.paid_fen), 1)
  return data.map((x: any, i: number) => `${20 + i / Math.max(data.length - 1, 1) * 660},${150 - x.paid_fen / max * 125}`).join(' ')
})
const latestAnswerMessage = computed(() => [...messages.value].reverse().find(x => x.role === 'assistant' && !x.error && x.text))
const latestAnswer = computed(() => latestAnswerMessage.value?.text || '')

function notify(message: string, error = false) { toast.value = message; toastError.value = error; setTimeout(() => {toast.value = ''}, 6000) }
async function task(fn: () => Promise<void>) {
  if (loading.value) return
  loading.value = true
  try { await fn() } catch (error) { notify(error instanceof Error ? error.message : '操作失败', true) }
  finally { loading.value = false }
}
async function loadProducts(reset = true) {
  if (reset) filters.offset = 0
  const data = await api('/products/search', 'POST', {query: filters.query, category: filters.category,
    max_price_fen: filters.maxPrice ? Math.round(Number(filters.maxPrice) * 100) : 10000000,
    size: filters.size, fit: filters.fit, material: filters.material, group_styles: true, offset: filters.offset, limit: 12})
  products.value = data.items; total.value = data.total; catalogCategories.value=data.categories||[]
}
async function loadView() {
  if (!user.value) return
  if (opsViews.includes(view.value) || view.value === 'platform') opsRevision.value++
  if (view.value === 'catalog') await loadProducts(false)
  if (view.value === 'orders') { orders.value = (await api('/orders')).items; afterSales.value = (await api('/after-sales')).items }
  if (view.value === 'analytics') stats.value = await api(`/analytics?days=${days.value}`)
  if (view.value === 'research') { rankings.value = await api(`/selection?category=${encodeURIComponent(filters.category)}&min_margin=${minMargin.value / 100}`); competitorRows.value = (await api('/competitors')).items }
  if (view.value === 'drafts') drafts.value = (await api('/drafts')).items
  if (view.value === 'audit') auditRows.value = (await api('/audit')).items
}
async function navigate(id: string) {
  if (id === view.value || navigating.value || loading.value || opsBusy.value || switchingShop.value) return
  navigating.value = true
  // Reset before replacing a long page so scroll anchoring cannot move the shell.
  mainScroll.value?.scrollTo({top: 0, behavior: 'instant'})
  if (window.innerWidth <= 650) window.scrollTo({top: 0, behavior: 'instant'})
  view.value = id; session.workspace = id; showLegacy.value = false
  try {
    await task(loadView)
    if (!activeChat.value.historyReady) await loadHistory()
  } finally { navigating.value = false }
}
async function legacyHistory() { showLegacy.value = !showLegacy.value; if (!historyReady.value) await loadHistory() }
async function openWorkspaceForm() {
  await nextTick()
  document.querySelector(view.value === 'ops-imports' ? '.report-importer' : view.value === 'platform' ? '.platform-page' : '.ops-workspace')?.scrollIntoView({behavior: 'smooth', block: 'start'})
  if (window.innerWidth < 980) chatOpen.value = false
}
async function platformChanged() {
  session.shopId='';user.value=await api('/auth/me')
  clearChats()
  await initializeHistory()
}
async function login() {
  await task(async () => {
    session.shopId = ''
    const data = await api('/auth/login', 'POST', loginForm)
    session.token = data.access_token; user.value = data.user
    loginForm.password = ''; view.value = 'today'; session.workspace = 'today'; clearChats()
    health.value = await api('/health'); await loadView(); await initializeHistory()
  })
}
async function logout() { await task(async () => { await api('/auth/logout', 'POST'); session.token = ''; session.shopId = ''; session.workspace = 'today'; clearChats(); user.value = null; selected.value = [] }) }
async function switchShop(shopId: string) {
  if (loading.value || opsBusy.value || anyChatting.value || historyLoading.value || switchingShop.value || shopId === (user.value?.shop_id || '')) return
  switchingShop.value = true
  const previous = session.shopId
  session.shopId = shopId
  try {
    const nextUser = await api('/auth/me')
    user.value = nextUser
    selected.value = []; compareOpen.value = false
    importOpen.value = false; importText.value = ''; sourceDocument.value = null
    products.value = []; orders.value = []; afterSales.value = []; drafts.value = []
    stats.value = null; rankings.value = null; competitorRows.value = []; auditRows.value = []
    showLegacy.value = false
    Object.assign(filters, {query: '', category: '', maxPrice: '', size: '', fit: '', material: '', offset: 0})
    await task(async () => { try { await loadView() } finally { await initializeHistory() } })
  } catch (error) {
    session.shopId = previous
    notify(error instanceof Error ? error.message : '切换店铺失败', true)
  } finally { switchingShop.value = false }
}
function selectShop(event: Event) { void switchShop((event.target as HTMLSelectElement).value) }
function chooseVariant(p: Product, event: Event) {
  const id = (event.target as HTMLSelectElement).value
  const variant = p.variants?.find(v => v.id === id)
  if (variant) Object.assign(p, variant)
}
function toggleProduct(p: Product) {
  const index = selected.value.findIndex(x => x.id === p.id)
  if (index >= 0) selected.value.splice(index, 1)
  else if (selected.value.length < 3) selected.value.push(p)
  else notify('最多比较3件商品')
}
async function showCompare() { await task(async () => { selected.value = (await api('/products/compare', 'POST', {product_ids: selected.value.map(p => p.id)})).items; compareOpen.value = true }) }
async function sendChat(prompt?: string, retryOf = '', repeatRequestId = '', originalTemporary = temporaryChat.value) {
  const query = (prompt || chatInput.value).trim()
  if (!query || anyChatting.value || historyLoading.value || switchingShop.value || !historyReady.value || showLegacy.value) return
  if (!aiReady.value && !retryOf && !repeatRequestId) {notify('请先在 Dify 中配置 DeepSeek 并绑定应用 API Key', true); return}
  const state = activeChat.value, workspace = activeWorkspace.value
  chatOpen.value = true; state.input = ''; state.chatting = true
  const shopId = user.value?.shop_id || null
  const stamp = new Date().toISOString()
  const requestId = repeatRequestId || globalThis.crypto?.randomUUID?.() ||
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
  state.messages.push({role: 'user', text: query, shop_id: shopId, created_at: stamp}, {role: 'assistant', text: '', shop_id: shopId, retryQuery: query, requestId, temporary: originalTemporary, created_at: stamp})
  const answer = state.messages[state.messages.length - 1]!
  try {
    await chatStream(query, state.conversationId, (event, data) => {
      if (event === 'start') {if (!data.replayed) state.offset += 1; answer.runId = data.run_id}
      if (event === 'conversation') state.conversationId = data.id
      if (event === 'session') state.sessionId=data.id
      if (event === 'memory') answer.memory=data.references||[]
      if (event === 'delta') answer.text += data.text
      if (event === 'replace') answer.text = data.text
      if (event === 'sources') answer.sources = data
      if (event === 'effects') answer.effects = data
      if (event === 'recovery') answer.recovery = data
      if (event === 'done' && data.status !== 'succeeded') answer.error = true
      if (event === 'failure') {answer.error = true; answer.text += `\n${data.message}`}
      if (activeChat.value === state) nextTick(() => chatScroll.value?.scrollTo({top: chatScroll.value.scrollHeight, behavior: 'smooth'}))
    }, workspace, state.sessionId, originalTemporary, requestId, retryOf)
    } catch (error) { answer.error = true; answer.text = [answer.text, error instanceof Error ? error.message : '对话失败'].filter(Boolean).join('\n') }
  finally {
    state.chatting = false
    if (!opsBusy.value) await task(loadView)
    else opsRevision.value++
  }
}
function retryMessage(message: Message) {
  sendChat(message.retryQuery, message.runId || '', message.runId ? '' : message.requestId || '', message.temporary ?? false)
}
async function saveAnswer() {await task(async () => {
  const sourceIds = [...new Set(latestAnswerMessage.value?.sources?.map(s => s.document_id) || [])]
  await api('/content', 'POST', {kind: contentKind.value, title: latestAnswer.value.slice(0, 60), content: latestAnswer.value, source_ids: sourceIds})
  notify('内容已保存至草稿'); if (view.value === 'drafts') await loadView()
})}
async function importSnapshots() {await task(async () => {const body = JSON.parse(importText.value); const data = await api('/competitors/import', 'POST', body); notify(`已导入 ${data.imported} 条快照`); importOpen.value = false; await loadView()})}
function historyMessages(items: any[]): Message[] {
  return [...items].reverse().flatMap(x => {
    const failed = ['failed', 'interrupted'].includes(x.status)
    const text = [x.answer?.trim(), failed ? x.error || '本次回复未完成，请重试。' : ''].filter(Boolean).join('\n')
    return [
      {role: 'user' as const, text: x.query, shop_id: x.shop_id ?? null, created_at: x.created_at},
      {role: 'assistant' as const, text: text || (x.status === 'running' ? '仍在处理中，请稍后刷新历史记录。' : '未收到完整回复'),
       shop_id: x.shop_id ?? null, error: failed, retryQuery: x.query, runId: x.id, temporary: x.memory_excluded,
       recovery: x.recovery, sources: x.sources || [], effects: x.effects || [], created_at: x.created_at}
    ]
  })
}
async function loadHistory(more = false) {
  if (chatting.value || historyLoading.value) return
  const previousHeight = chatScroll.value?.scrollHeight || 0
  const previousTop = chatScroll.value?.scrollTop || 0
  const state = activeChat.value, workspace = activeWorkspace.value
  state.historyLoading = true; state.historyError = ''
  try {
    const data = await api(`/chat/runs?workspace=${workspace}&offset=${more ? state.offset : 0}`)
    const restored = historyMessages(data.items)
    state.messages = more ? [...restored, ...state.messages] : restored
    state.offset = (more ? state.offset : 0) + data.items.length
    state.hasMore = data.has_more
    if (!more) {state.conversationId = data.items[0]?.conversation_id || '';state.sessionId=data.items[0]?.session_id||''}
    state.historyReady = true
  } catch (error) {state.historyError = error instanceof Error ? error.message : '历史记录加载失败'}
  finally {
    state.historyLoading = false
    await nextTick()
    if (activeChat.value === state && chatScroll.value) chatScroll.value.scrollTo({
      top: more ? previousTop + chatScroll.value.scrollHeight - previousHeight : chatScroll.value.scrollHeight,
      behavior: 'instant'
    })
  }
}
async function initializeHistory() {
  messages.value = []; conversationId.value = ''; historyReady.value = false
  messageHasMore.value = false; messageOffset.value = 0
  await loadHistory()
}
async function viewSource(id: string) { await task(async () => { sourceDocument.value = await api(`/knowledge/documents/${id}`) }) }
function escapeModal(event:KeyboardEvent) {
  if(event.key==='Escape'&&!loading.value){compareOpen.value=false;sourceDocument.value=null;importOpen.value=false}
}
onMounted(async () => {
  window.addEventListener('keydown',escapeModal)
  try {health.value = await api('/health'); if (session.token) {user.value = await api('/auth/me'); if (!navigation.some(n => n.id === view.value)) view.value = 'today'; await task(async () => {await loadView(); await initializeHistory()})}}
  catch {session.token = ''; session.shopId = ''; user.value = null}
})
onUnmounted(()=>window.removeEventListener('keydown',escapeModal))
</script>

<template>
  <div v-if="!user" class="login-page">
    <section class="login-story"><div class="brand"><span class="brand-mark">序</span><span>衣序 <small>COMMERCE COPILOT</small></span></div>
      <div class="login-copy"><div class="eyebrow">让每日运营，都有依据</div><h1>专注运营，<br>让经营持续改善。</h1><p>连接商品、内容与经营数据。<br>统一分析授权店铺，创作文案，跟进经营问题。</p><div class="login-pills"><span>店铺运营</span><span>内容创作</span><span>经营洞察</span></div></div>
      <div class="login-foot">DIFY × DEEPSEEK <span>山序男装 × 知织女装 · 双店演示</span></div>
    </section>
    <section class="login-panel"><div class="login-form"><span class="eyebrow">WELCOME TO YOUR WORKSPACE</span><h2>进入运营工作台</h2><p class="muted">登录运营账号，管理授权店铺。</p>
      <form @submit.prevent="login"><label>运营账号<input v-model="loginForm.username" autocomplete="username" required placeholder="operator"/></label><p class="muted">登录后可查看全部授权店铺或筛选单店。</p><label>登录密码<input v-model="loginForm.password" type="password" autocomplete="current-password" required placeholder="输入本地配置的演示密码"/></label><button class="primary wide" :disabled="loading"><LoaderCircle v-if="loading" class="spin" :size="18"/>进入工作台<ArrowRight :size="18"/></button></form>
      <div class="login-help"><ShieldCheck :size="18"/><div>本地演示环境<p>初始密码保存在项目的 .env 文件中。内置合成数据用于测试，正式数据可在平台接入页配置。</p></div></div>
      <a href="http://localhost:8088" target="_blank" rel="noopener">打开 Dify 配置 <ExternalLink :size="14"/></a>
    </div></section>
  </div>
  <div v-else class="shell" :data-shop="user.shop_id" :class="{'chat-hidden': !chatOpen}">
    <aside class="sidebar"><div class="brand"><span class="brand-mark">序</span><span>衣序<small>COMMERCE COPILOT</small></span></div>
      <select class="shop-switcher" :value="user.shop_id || ''" aria-label="店铺范围" :disabled="loading || opsBusy || anyChatting || historyLoading || switchingShop" @change="selectShop"><option value="">{{user.shops.some(s=>s.mode==='live')?'全部正式店铺':'全部授权店铺'}}</option><option v-for="shop in user.shops" :key="shop.id" :value="shop.id">{{shop.name}}</option></select>
      <nav aria-label="工作台栏目"><button v-for="item in navigation" :key="item.id" :class="{active: view === item.id}" :aria-current="view === item.id ? 'page' : undefined" :disabled="navigating || loading || opsBusy || switchingShop" @click="navigate(item.id)"><component :is="item.icon" :size="18"/><span>{{item.label}}</span><LoaderCircle v-if="chatStates[`${user.id}:${user.shop_id || 'all'}:${item.id}`]?.chatting" :size="12" class="spin"/></button></nav>
      <div class="sidebar-bottom"><div class="avatar">运</div><div><strong>{{user.display_name}}</strong><small>运营账号 · 已登录</small></div><button class="icon-button" aria-label="退出登录" :disabled="loading || opsBusy || anyChatting || historyLoading" @click="logout"><LogOut :size="17"/></button></div>
    </aside>
    <main ref="mainScroll" class="main"><header class="topbar"><div>工作台 <ChevronRight :size="13"/><span>{{navigation.find(x => x.id === view)?.label}}</span></div><div><span class="demo-badge"><i></i>{{platformSource}}</span><span v-if="navigating" class="navigation-status" role="status">正在切换…</span><button class="icon-button" aria-label="刷新当前数据" :disabled="loading" @click="task(loadView)"><RefreshCw :size="17" :class="{spin: loading}"/></button><button class="icon-button" aria-label="切换智能助手" @click="chatOpen = !chatOpen"><PanelRightOpen v-if="!chatOpen" :size="19"/><PanelRightClose v-else :size="19"/></button></div></header>
      <div class="main-content"><section class="page-heading"><div><div class="eyebrow">YOUR OPERATIONS, IN FOCUS</div><h1>{{title}}</h1><p>{{subtitle}}</p></div></section>

      <PlatformConnections v-if="view==='platform'" :shop-id="user.shop_id" :shops="user.shops" :refresh-key="opsRevision" @changed="platformChanged" @busy="opsBusy=$event"/>
      <OpsWorkspace v-if="opsViews.includes(view)" :key="user.shop_id || 'all'" :view="view" :shop-id="user.shop_id" :shops="scopedShops" :refresh-key="opsRevision" @ask="sendChat" @busy="opsBusy=$event"/>

      <template v-if="view === 'catalog'">
        <section class="shop-directory" aria-label="店铺列表"><button :class="{active: !user.shop_id}" :disabled="loading || chatting || historyLoading || switchingShop" @click="switchShop('')"><ShoppingBag :size="20"/><span><strong>全部授权店铺</strong><small>汇总授权店铺商品</small></span></button><button v-for="shop in user.shops" :key="shop.id" :class="{active: user.shop_id === shop.id}" :disabled="loading || chatting || historyLoading || switchingShop" @click="switchShop(shop.id)"><Store :size="20"/><span><strong>{{shop.name}}</strong><small>{{shop.positioning}}</small></span></button></section>
        <form class="filters" @submit.prevent="task(() => loadProducts())"><div class="search-field"><SearchIcon :size="17"/><input v-model="filters.query" aria-label="搜索商品" placeholder="搜索商品名称"/></div><select v-model="filters.category" aria-label="商品分类" @change="task(() => loadProducts())"><option value="">全部品类</option><option v-for="c in categories" :key="c">{{c}}</option></select><input v-model="filters.maxPrice" type="number" min="0" aria-label="最高售价" placeholder="最高售价 / 元"/><select v-model="filters.size" aria-label="服装尺码"><option value="">全部尺码</option><option v-for="size in user?.shop.sizes || []" :key="size">{{size}}</option></select><select v-model="filters.fit" aria-label="服装版型"><option value="">全部版型</option><option v-for="fit in ['合体','宽松','直筒','收腰','阔腿','锥形','A字']" :key="fit">{{fit}}</option></select><select v-model="filters.material" aria-label="服装面料"><option value="">全部面料</option><option v-for="fabric in ['棉','羊毛','亚麻','粘纤','锦纶','聚酯纤维']" :key="fabric">{{fabric}}</option></select><button class="secondary" :disabled="loading">筛选</button></form>
        <div class="section-label"><h3>商品资料 <span>{{total}} 款匹配服装</span></h3><button v-if="selected.length" class="text-button" @click="showCompare"><Scale :size="16"/>对比已选 {{selected.length}} 件</button></div>
        <div v-if="products.length" class="product-grid"><article v-for="(p, i) in products" :key="p.id" class="product-card"><div class="product-image" :class="`tone-${i % 4}`"><span class="product-category">{{p.category}}</span><GarmentArt :category="p.category" :color="p.specs['颜色']"/><button class="compare-check" :class="{checked: selected.some(x => x.id === p.id)}" :aria-label="`选择对比 ${p.title}`" @click="toggleProduct(p)"><Check v-if="selected.some(x => x.id === p.id)" :size="14"/><Plus v-else :size="14"/></button></div><div class="product-info"><small>{{p.shop_name}} · {{p.specs['款号'] || '新品'}}</small><h3>{{p.style_title || p.title}}</h3><div class="spec-line">{{p.specs['面料']}} · {{p.specs['版型']}}</div><div class="size-chips"><span v-for="size in p.available_sizes || [p.specs['尺码']]" :key="size">{{size}}</span><small>{{(p.available_colors || [p.specs['颜色']]).join(' / ')}}</small></div><select v-if="p.variants?.length" class="variant-select" :aria-label="`选择规格 ${p.style_title}`" :value="p.id" @change="chooseVariant(p, $event)"><option v-for="v in p.variants" :key="v.id" :value="v.id">{{v.specs['颜色']}} / {{v.specs['尺码']}} · 余{{v.stock}}件</option></select><div class="product-footer"><strong>{{money(p.price_fen)}}</strong><span>可选库存 {{p.style_stock ?? p.stock}}</span></div><button class="product-ask" @click="sendChat(`查询商品 ${p.id} 的事实资料，写一段短视频口播草稿并列出依据，不编造优惠`) ">生成文案 <ArrowUpRight :size="14"/></button></div></article></div>
        <div v-else class="empty"><SearchIcon :size="32"/><h3>没有找到符合条件的商品</h3><p>调整售价、尺码、版型或分类。</p></div>
        <div class="pagination"><button class="secondary" :disabled="!filters.offset || loading" @click="filters.offset -= 12; task(() => loadProducts(false))">上一页</button><span>{{Math.floor(filters.offset / 12) + 1}} / {{Math.max(1, Math.ceil(total / 12))}}</span><button class="secondary" :disabled="filters.offset + 12 >= total || loading" @click="filters.offset += 12; task(() => loadProducts(false))">下一页</button></div>
      </template>

      <template v-if="view === 'orders'"><div class="section-label"><h3>最近订单 <span>仅显示当前授权店铺的最近30条订单</span></h3></div><div v-for="o in orders" :key="[o.shop_id,o.id].join(':')" class="order-card"><div class="order-top"><span>{{user.shops.find(s=>s.id===o.shop_id)?.name||o.shop_id}} · {{o.id}}</span><span class="status">{{statusNames[o.status] || o.status}}</span></div><div class="order-body"><div class="order-icon"><Package :size="25"/></div><div><h3>{{o.title}}</h3><p>{{date(o.paid_at || o.created_at)}} · 数量 {{o.quantity}} · {{o.specs_snapshot?.['颜色']}} / {{o.specs_snapshot?.['尺码']}}</p><small v-if="o.refund_reason">退款原因：{{o.refund_reason}}</small></div><strong>{{money(o.paid_fen)}}</strong></div><div class="order-bottom"><small>物流状态为演示记录</small></div></div><div class="section-label"><h3>售后记录</h3></div><div v-for="s in afterSales" :key="[s.shop_id,s.id].join(':')" class="record-card"><div><strong>{{shopNames[s.shop_id]}} · {{s.order_id}}</strong><p>{{s.reason}} · {{s.quantity == null ? '退款记录' : s.quantity + ' 件'}}</p><small>{{s.id}}</small></div><span class="status">{{statusNames[s.status] || s.status}}</span></div><div v-if="!afterSales.length" class="empty compact"><p>当前店铺范围暂无售后处理记录</p></div></template>

      <template v-if="view === 'analytics' && stats"><div class="section-label"><h3>经营快照 <span>完整自然日 · 上海时区</span></h3><select v-model="days" aria-label="统计周期" @change="task(loadView)"><option :value="7">最近7天</option><option :value="30">最近30天</option><option :value="90">最近90天</option></select></div><div class="metric-grid"><article><small>支付成交额</small><strong>{{money(stats.paid_fen)}}</strong><span :class="stats.growth_percent >= 0 ? 'positive' : 'negative'">{{stats.growth_percent == null ? '上期无可比基数' : `${stats.growth_percent > 0 ? '+' : ''}${stats.growth_percent}% 较上期`}}</span></article><article><small>支付订单数</small><strong>{{stats.order_count}}<em>单</em></strong><span>按支付时间统计</span></article><article><small>客单价</small><strong>{{money(stats.aov_fen)}}</strong><span>成交额 ÷ 支付订单数</span></article><article><small>期间退款额</small><strong>{{money(stats.refund_fen)}}</strong><span>按退款发生时间统计</span></article></div>
        <section class="panel"><div class="section-label"><h3>成交趋势</h3><span class="chart-key"><i></i>支付成交额</span></div><svg class="trend-chart" viewBox="0 0 700 180" role="img" aria-label="每日支付成交额趋势"><line v-for="y in [25, 75, 125, 165]" :key="y" x1="20" :y1="y" x2="680" :y2="y" stroke="#e8ece8" stroke-dasharray="4 5"/><polygon :points="`20,165 ${chartPoints} 680,165`" fill="url(#fade)"/><polyline :points="chartPoints" fill="none" stroke="#25816b" stroke-width="3" stroke-linejoin="round"/><defs><linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#51a58a" stop-opacity=".25"/><stop offset="100%" stop-color="#51a58a" stop-opacity="0"/></linearGradient></defs></svg><div class="chart-dates"><span>{{stats.trend[0]?.date}}</span><span>{{stats.trend.at(-1)?.date}}</span></div></section>
        <div class="two-columns"><section class="panel"><div class="section-label"><h3>渠道表现</h3></div><div v-for="c in stats.channels" :key="c.channel" class="channel"><div><span>{{c.channel}}</span><strong>{{money(c.paid_fen)}}</strong></div><div class="bar"><i :style="{width: `${Math.max(2, c.paid_fen / Math.max(stats.paid_fen, 1) * 100)}%`}"></i></div><small>{{c.orders}} 笔订单</small></div></section><section class="insight-panel"><Sparkles :size="24"/><h3>让数字变成行动</h3><p>让助手结合渠道、退款和商品数据，整理一份有依据的经营观察。</p><button class="primary" @click="sendChat(`请分析最近${days}个完整自然日的经营表现，区分数据事实、待验证的假设与建议。`) ">生成经营观察 <ArrowUpRight :size="16"/></button><small>净收款 {{money(stats.net_receipts_fen)}}，不等于利润。</small></section></div><div class="two-columns apparel-metrics"><section class="panel"><div class="section-label"><h3>尺码销量 <span>按支付订单统计件数</span></h3></div><div v-for="s in stats.size_sales" :key="s.size" class="channel"><div><span>{{s.size}}</span><strong>{{s.units}} 件</strong></div><div class="bar"><i :style="{width: `${s.units / Math.max(...stats.size_sales.map((x: any) => x.units), 1) * 100}%`}"></i></div></div></section><section class="panel"><div class="section-label"><h3>退款原因 <span>按退款发生时间</span></h3></div><div v-for="r in stats.refund_reasons" :key="r.reason" class="refund-reason"><span>{{r.reason}}<small>{{r.orders}} 单</small></span><strong>{{money(r.refund_fen)}}</strong></div><p v-if="!stats.refund_reasons.length" class="muted">本期没有退款记录</p></section></div><p class="footnote">{{stats.notes.join(' ')}} 数据更新于 {{date(stats.as_of)}}。</p></template>

      <section v-if="view === 'analytics' && stats && !user.shop_id" class="panel shop-performance"><div class="section-label"><h3>各店经营对比 <span>同一统计周期</span></h3></div><div class="table-wrap"><table><thead><tr><th>店铺</th><th>支付成交额</th><th>订单数</th><th>期间退款额</th><th></th></tr></thead><tbody><tr v-for="shop in stats.by_shop" :key="shop.shop_id"><td>{{shop.shop_name}}</td><td>{{money(shop.paid_fen)}}</td><td>{{shop.order_count}}</td><td>{{money(shop.refund_fen)}}</td><td><button class="text-button" :disabled="loading || chatting || historyLoading || switchingShop" @click="switchShop(shop.shop_id)">查看店铺<ArrowRight :size="14"/></button></td></tr></tbody></table></div></section>

      <template v-if="view === 'research'"><form class="filters" @submit.prevent="task(loadView)"><select v-model="filters.category" aria-label="选品分类"><option value="">全部品类</option><option v-for="c in categories" :key="c">{{c}}</option></select><label class="inline-label">最低毛利率 <input v-model.number="minMargin" type="number" min="0" max="99" required/> %</label><button class="secondary" :disabled="loading">重新评估</button><button type="button" class="text-button" @click="importOpen = true"><Plus :size="16"/>导入竞品快照</button></form><section class="panel" v-if="rankings"><div class="section-label"><h3>本次优选 <span>评估 {{rankings.evaluated}} 件 · 规则版本 demo-v1</span></h3></div><div v-for="(r, i) in rankings.items" :key="r.product.id" class="rank-row"><span class="rank-index">{{String(Number(i) + 1).padStart(2, '0')}}</span><div class="rank-main"><h3>{{r.product.title}}</h3><small>{{r.product.shop_name}} · {{r.product.id}} · {{money(r.product.price_fen)}} · 近30天 {{r.observed_units}} 件</small><div class="score-parts"><span v-for="(v, k) in r.parts" :key="k">{{({demand: '需求', margin: '毛利', competition: '价格机会', supply: '供货', quality: '质量'} as Record<string, string>)[k]}} {{v}}</span></div></div><div class="rank-score"><strong>{{r.score}}</strong><small>综合分 / 100</small></div></div><div v-if="!rankings.items.length" class="empty compact">没有符合条件且资料完整的候选</div><details v-if="rankings.incomplete.length" class="data-details"><summary>查看资料不足的候选（{{rankings.incomplete.length}}）</summary><p v-for="r in rankings.incomplete" :key="r.product.id">{{r.product.title}}：缺少 {{r.missing.join('、')}}</p></details><p class="footnote">{{rankings.notes.join(' ')}}</p></section><section class="panel"><div class="section-label"><h3>竞品资料库 <span>最近导入的100条快照</span></h3></div><div class="table-wrap"><table><thead><tr><th>店铺</th><th>商品</th><th>参考商品</th><th>价格</th><th>采集时间</th><th>来源</th></tr></thead><tbody><tr v-for="c in competitorRows.slice(0, 20)" :key="c.id"><td>{{shopNames[c.shop_id]}}</td><td>{{c.title}}</td><td>{{c.product_id}}</td><td>{{money(c.price_fen)}}</td><td>{{date(c.captured_at)}}</td><td><span :class="['status', c.is_demo ? '' : 'neutral']">{{c.is_demo ? '合成快照' : '导入快照'}}</span></td></tr></tbody></table></div></section></template>

      <template v-if="view === 'drafts'"><div class="section-label"><h3>已保存的内容 <span>{{drafts.length}} 份</span></h3><button v-if="latestAnswer" class="secondary" @click="saveAnswer">保存最近一次助手回复</button></div><article class="draft-card" v-for="d in drafts" :key="d.id"><div class="draft-icon"><FileText :size="22"/></div><div class="draft-main"><span class="eyebrow">{{({marketing: '营销文案', report: '经营报告'} as Record<string,string>)[d.kind]}}</span><h3>{{d.payload.title}}</h3><p class="draft-content">{{d.payload.description || d.payload.content}}</p><small>{{d.shop_id ? user.shops.find(s=>s.id===d.shop_id)?.name||d.shop_id : '全部授权店铺'}} · v{{d.version}} · {{date(d.created_at)}} · {{statusNames[d.status]}}</small></div></article><div v-if="!drafts.length" class="empty"><FileText :size="32"/><h3>让第一份内容在这里落地</h3><p>将运营 Agent 的文案和经营分析保存为草稿。</p></div></template>


      <template v-if="view === 'audit'"><section class="panel"><div v-for="a in auditRows" :key="a.request_id + a.operation" class="audit-row"><span class="audit-dot"></span><div><strong>{{a.operation}}</strong><p>{{a.details.status || a.details.kind || a.details.result?.message || '操作已记录'}}</p><small>{{a.request_id}}</small></div><time>{{date(a.created_at)}}</time></div><div v-if="!auditRows.length" class="empty compact">暂无操作记录</div></section></template>
      <footer class="main-footer"><span>衣序 · Commerce Copilot</span><span>经营有依据，过程可追溯。</span></footer></div>
    </main>

    <aside v-if="chatOpen" class="chat-panel" :data-chat-scope="user.shop_id || 'all'" :data-chat-workspace="activeWorkspace"><header><div class="assistant-icon"><Sparkles :size="20"/></div><div><h3>衣序运营 Agent</h3><span>{{chatWorkspaceName}} · DeepSeek</span></div><button class="icon-button" aria-label="关闭助手" @click="chatOpen = false"><X :size="17"/></button></header><div class="chat-meta"><span :class="['connection-dot', {ready: aiReady}]"></span>{{aiReady ? 'Dify 应用已绑定' : '等待配置 DeepSeek / 应用 Key'}}<span class="chat-scope-badge">{{chatScopeName(user.shop_id)}}</span><button class="text-button" :disabled="historyLoading" @click="legacyHistory">{{showLegacy?'返回栏目对话':'旧版总对话'}}</button></div>
      <div class="chat-memory-controls"><button class="text-button" @click="memoryOpen=true">记忆管理</button><label><input type="checkbox" v-model="temporaryChat" :disabled="anyChatting"/>临时对话</label></div>
      <div class="workspace-chat-note"><span>{{showLegacy?'旧版记录仅供查阅':'本栏目的对话独立保存，切回来可继续'}}</span><button v-if="!showLegacy && ['ops-imports','ops-videos','platform'].includes(view)" class="text-button" @click="openWorkspaceForm">{{view==='ops-imports'?'上传与校验报表':view==='platform'?'填写配置与授权':'上传与管理素材'}}<ArrowUpRight :size="12"/></button></div>
      <p v-if="anyChatting && !chatting" class="workspace-chat-running" role="status">其他栏目的回复正在生成，可以浏览，完成后再发送新消息。</p>
      <div v-if="historyError" class="history-error" role="alert">{{historyError}}<button class="text-button" :disabled="chatting || historyLoading" @click="loadHistory(historyReady)">重试</button></div>
      <div class="chat-messages" ref="chatScroll">
        <button v-if="messageHasMore" class="text-button older-messages" :disabled="chatting || historyLoading" @click="loadHistory(true)">加载更早消息</button>
        <p v-if="historyLoading" class="muted history-loading">正在加载对话…</p>
        <div v-if="historyReady && !messages.length" class="chat-welcome">
          <div class="welcome-spark"><Sparkles :size="31"/></div><h2>今天，想解决什么？</h2>
          <p>告诉我你的需求，<br>我们一起找到下一步。</p>
          <button v-for="p in prompts" :key="p" :disabled="anyChatting || showLegacy" @click="sendChat(p)">{{p}}<ArrowUpRight :size="15"/></button>
          <div v-if="!aiReady" class="setup-notice"><CircleHelp :size="17"/><p>先完成 Dify 中的模型配置与应用发布。商品、订单和报表功能现在就可以使用。<a href="http://localhost:8088" target="_blank" rel="noopener">打开 Dify <ExternalLink :size="12"/></a></p></div>
        </div>
        <article v-for="(m, i) in messages" :key="i" class="chat-message" :data-chat-scope="m.shop_id || 'all'" :class="[m.role, {error: m.error}]">
          <small><span>{{m.role === 'user' ? '你' : '衣序助手'}} · {{chatScopeName(m.shop_id)}}</span><time v-if="m.created_at">{{date(m.created_at)}}</time></small>
          <div class="message-bubble">{{m.text || '正在处理…'}}</div>
          <div v-if="m.memory?.length" class="message-sources"><button v-for="memory in m.memory" :key="memory.id" @click="memoryOpen=true">已使用记忆：{{memory.title}}</button></div>
          <ul v-if="m.effects?.length" class="message-effects" aria-label="已执行的操作"><li v-for="(effect, effectIndex) in m.effects" :key="effectIndex"><Check :size="12"/>{{effect.label}}<small v-if="effect.resource_id">{{effect.resource_id}}</small></li></ul>
          <p v-if="m.recovery && (m.error || m.recovery.business_status === 'unknown')" class="message-recovery" role="status">{{m.recovery.message}}</p>
          <button v-if="m.error && m.retryQuery && !showLegacy" class="text-button" :disabled="anyChatting || historyLoading || switchingShop" @click="retryMessage(m)">{{m.recovery?.can_retry === false ? '核对执行结果' : '重试这条消息'}}</button>
          <div v-if="m.sources?.length" class="message-sources"><button v-for="source in m.sources" :key="source.document_id" @click="viewSource(source.document_id)"><FileText :size="12"/>{{source.title}}</button></div>
        </article>
      </div>
      <div v-if="latestAnswer" class="save-answer"><select v-model="contentKind" aria-label="保存内容类型"><option value="report">经营报告</option><option value="marketing">营销文案</option></select><button class="text-button" :disabled="loading || anyChatting || historyLoading" @click="saveAnswer"><FileText :size="14"/>保存回复</button></div><form v-if="!showLegacy" class="chat-composer" @submit.prevent="sendChat()"><textarea v-model="chatInput" aria-label="给助手发送消息" rows="3" :placeholder="`向${chatWorkspaceName}助手描述需求…`" :disabled="chatting || historyLoading || !historyReady" @keydown.ctrl.enter.prevent="sendChat()"></textarea><div><span>Ctrl + Enter 发送</span><button class="primary icon-button" :disabled="anyChatting || historyLoading || !historyReady || !chatInput.trim() || !aiReady" aria-label="发送消息"><LoaderCircle v-if="chatting" :size="17" class="spin"/><Send v-else :size="17"/></button></div></form><p class="chat-footnote">关键金额与状态以业务记录为准</p></aside>
  </div>

  <div v-if="toast" class="toast" :class="{error: toastError}" role="status">{{toast}}</div>
  <MemoryManager v-if="memoryOpen&&user" :key="`${user.id}:${user.shop_id||'all'}:${activeWorkspace}`" :workspace="activeWorkspace" :shop-id="user.shop_id" @close="memoryOpen=false" @changed="memoryChanged"/>
  <div v-if="compareOpen" class="modal-backdrop" @click.self="compareOpen = false"><section class="modal large"><button class="modal-close icon-button" aria-label="关闭" @click="compareOpen = false"><X :size="19"/></button><span class="eyebrow">SIDE BY SIDE</span><h2>商品规格与库存对比</h2><div class="table-wrap"><table class="comparison"><tbody><tr><th>商品</th><td v-for="p in selected" :key="p.id">{{p.title}}</td></tr><tr><th>价格</th><td v-for="p in selected" :key="p.id">{{money(p.price_fen)}}</td></tr><tr v-for="key in [...new Set(selected.flatMap(p => Object.keys(p.specs)))]" :key="key"><th>{{key}}</th><td v-for="p in selected" :key="p.id">{{p.specs[key] ?? '未知'}}</td></tr><tr><th>库存</th><td v-for="p in selected" :key="p.id">{{p.stock}}</td></tr></tbody></table></div><p class="footnote">价格和库存已在打开对比时刷新。</p></section></div>
  <div v-if="sourceDocument" class="modal-backdrop" @click.self="sourceDocument = null"><section class="modal large"><button class="modal-close icon-button" aria-label="关闭资料" @click="sourceDocument = null"><X :size="19"/></button><span class="eyebrow">资料原文 · {{sourceDocument.version}}</span><h2>{{sourceDocument.title}}</h2><pre class="source-text">{{sourceDocument.text}}</pre></section></div>
  <div v-if="importOpen" class="modal-backdrop" @click.self="importOpen = false"><form class="modal large" @submit.prevent="importSnapshots"><button type="button" class="modal-close icon-button" aria-label="关闭" @click="importOpen = false"><X :size="19"/></button><h2>导入竞品快照</h2><p class="muted">粘贴 JSON 数组。需包含参考商品 ID、规格、价格（分）、来源和带时区的采集时间。</p><textarea class="code-input" v-model="importText" rows="12" required placeholder='[{"product_id":"MEN-001-01-M","title":"竞品样本","price_fen":13900,"specs":{"尺码":"M","面料":"100%棉"},"source":"https://example.com/product","captured_at":"2026-10-15T10:00:00+08:00","currency":"CNY"}]'></textarea><button class="primary wide" :disabled="loading">校验并导入<ArrowRight :size="16"/></button></form></div>
</template>


