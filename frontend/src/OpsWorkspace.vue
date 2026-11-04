<script setup lang="ts">
import {computed, onMounted, onUnmounted, reactive, ref, watch} from 'vue'
import {api, rawApi} from './api'
import ReportImporter from './ReportImporter.vue'
import ScriptReview from './ScriptReview.vue'

const props = defineProps<{view: string; shopId: string | null; shops: {id: string; name: string}[]; refreshKey: number}>()
const emit = defineEmits<{ask: [query: string]; busy: [value: boolean]}>()
const busy = ref(false), message = ref(''), failed = ref(false)
const loaded = ref(false)
const summary = ref<any>(null), tasks = ref<any[]>([]), report = ref<any>(null)
const videos = ref<any[]>([]), peers = ref<any[]>([]), assets = ref<any[]>([]), products = ref<any[]>([])
const scripts = ref<any[]>([]), jobs = ref<any[]>([]), imports = ref<any[]>([]), schedules = ref<any[]>([])
const kinds = ref<string[]>([])
const workDate = ref(new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date()))
const days = ref(7), selectedShop = ref(props.shopId || props.shops[0]?.id || '')
const taskEdit = ref<any>(null), scriptEdit = ref<any>(null)
const peerForm = reactive({title:'', transcript:'', asset_id:'', source:''})
const scriptForm = reactive({product_id:'', reference_id:'', brief:'围绕通勤场景，生成三段简洁口播，说明商品事实。'})
const assetGroup = ref('MEN-001'), mediaUrl = ref(''), mediaTitle = ref('')
let alive = true, generation = 0
const name = (id: string) => props.shops.find(s=>s.id===id)?.name || id
const money = (value: number | null | undefined) => value == null ? '暂无数据' : `¥${(value/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}`
const percent = (value: number | null | undefined) => value == null ? '暂无数据' : `${(value*100).toFixed(2)}%`
const statuses: Record<string,string> = {pending:'待处理',in_progress:'进行中',blocked:'阻塞',review:'待验收',completed:'已完成',cancelled:'已取消',open:'待处理',resolved:'已解决',queued:'排队中',running:'剪辑中',succeeded:'已完成',failed:'失败'}
const kindNames: Record<string,string> = {shops:'店铺资料',staff:'人员资料',products:'商品资料',orders:'订单明细',refunds:'售后退款',daily_reports:'经营日报',inventory:'库存',videos:'视频清单',video_daily:'视频日指标',sop_templates:'SOP模板',tasks:'运营任务',peer_references:'同行参考'}
const dueTime = (value:string) => new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value))
const activeTasks = computed(()=>tasks.value.filter(t=>t.date===workDate.value))
const scopedAssets = computed(()=>assets.value.filter(a=>a.shop_id===selectedShop.value))
const scriptProducts = computed(()=>products.value.filter(p=>p.shop_id===selectedShop.value && scopedAssets.value.some(a=>a.group_id===p.group_id)))
const shopPeers = computed(()=>peers.value.filter(p=>p.shop_id===selectedShop.value))
const taskCounts = computed(()=>Object.fromEntries(['pending','in_progress','blocked','completed'].map(s=>[s,activeTasks.value.filter(t=>t.status===s).length])))

async function act(fn:()=>Promise<void>) {
  if(busy.value) return
  busy.value=true; message.value='';failed.value=false
  try {await fn()} catch(e) {failed.value=true;message.value=e instanceof Error?e.message:'操作失败'}
  finally {if(alive)busy.value=false}
}
async function load() {
  if(!workDate.value)throw new Error('请选择工作日期')
  const version=++generation
  const requests = [
    api(`/ops/overview?days=${days.value}`), api(`/ops/tasks?day=${workDate.value}`),
    api(`/ops/daily-report?day=${workDate.value}`),api('/ops/videos'),api('/ops/peers'),
    api('/ops/assets'),api('/ops/scripts'),api('/ops/jobs'),api('/ops/imports'),api('/ops/schedules')
  ]
  const result=await Promise.all(requests)
  if(!alive||generation!==version)return
  summary.value=result[0];tasks.value=result[1].items;report.value=result[2]
  videos.value=result[3].items;peers.value=result[4].items;assets.value=result[5].items;products.value=result[5].products
  scripts.value=result[6].items;jobs.value=result[7].items;imports.value=result[8].items;kinds.value=result[8].kinds
  schedules.value=result[9].items
  loaded.value=true
  if(!['a','b'].includes(selectedShop.value)&&!products.value.some(p=>p.shop_id===selectedShop.value&&p.group_id===assetGroup.value))assetGroup.value=products.value.find(p=>p.shop_id===selectedShop.value)?.group_id||''
  if(!scriptForm.product_id || !scriptProducts.value.some(p=>p.sku_id===scriptForm.product_id))scriptForm.product_id=scriptProducts.value[0]?.sku_id || ''
}
async function demo() {await act(async()=>{const result=await api('/ops/demo/import','POST',{});await load();message.value=result.message})}
async function generateTasks() {await act(async()=>{const result=await api('/ops/tasks/generate','POST',{date:workDate.value});await load();message.value=`新增 ${result.created} 项任务，已有进度保留。`})}
async function saveTask() {await act(async()=>{const t=taskEdit.value;await api(`/ops/tasks/${t.id}`,'PATCH',{expected_version:t.version,status:t.status,owner:t.owner,evidence:t.evidence,note:t.note});taskEdit.value=null;await load();message.value='任务进度已保存'})}
async function upload(event:Event){const file=(event.target as HTMLInputElement).files?.[0];if(!file)return;await act(async()=>{await rawApi(`/ops/assets/upload?shop_id=${encodeURIComponent(selectedShop.value)}&group_id=${encodeURIComponent(assetGroup.value)}&filename=${encodeURIComponent(file.name)}`,file);await load();message.value='素材已上传并读取时长与音轨';(event.target as HTMLInputElement).value=''})}
async function openMedia(path:string,title:string){await act(async()=>{const response=await rawApi(path);const url=URL.createObjectURL(await response.blob());if(!alive){URL.revokeObjectURL(url);return}if(mediaUrl.value)URL.revokeObjectURL(mediaUrl.value);mediaUrl.value=url;mediaTitle.value=title})}
function closeMedia(){if(mediaUrl.value)URL.revokeObjectURL(mediaUrl.value);mediaUrl.value=''}
async function download(path:string,filename:string){await act(async()=>{const response=await rawApi(path);const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url)})}
async function createPeer(){await act(async()=>{await api('/ops/peers','POST',{...peerForm,shop_id:selectedShop.value,asset_id:peerForm.asset_id||null,source:peerForm.source||'用户提供'});Object.assign(peerForm,{title:'',transcript:'',asset_id:'',source:''});await load();message.value='同行参考已保存'})}
async function analyzePeer(id:string){await act(async()=>{await api(`/ops/peers/${id}/analyze`,'POST',{});await load();message.value='拆解报告已生成并保存'})}
async function generateScript(){await act(async()=>{await api('/ops/scripts/generate','POST',{...scriptForm,reference_id:scriptForm.reference_id||null});await load();message.value='脚本已生成，可编辑分镜后开始剪辑'})}
function editScript(s:any){scriptEdit.value={...s,segments:s.payload.segments.map((part:any)=>({...part}))}}
async function saveScript(){await act(async()=>{const s=scriptEdit.value;await api(`/ops/scripts/${s.id}`,'PATCH',{expected_version:s.version,title:s.title,product_id:s.payload.product_id,reference_id:s.payload.reference_id||null,segments:s.segments});scriptEdit.value=null;await load();message.value='新脚本版本已保存，历史剪辑仍对应原版本'})}
async function renderScript(s:any){await act(async()=>{const j=await api(`/ops/scripts/${s.id}/render`,'POST',{expected_version:s.version});await load();message.value=`剪辑任务${statuses[j.status]||j.status}，可在下方查看进度。`})}
async function retryJob(id:string){await act(async()=>{await api(`/ops/jobs/${id}/retry`,'POST',{});await load();message.value='已重新排队'})}
async function saveSchedule(s:any){await act(async()=>{await api(`/ops/schedules/${s.shop_id}`,'PUT',{enabled:s.enabled,hour:Number(s.hour)});await load();message.value='每日SOP计划已保存（上海时区）'})}
async function saveReport(){await act(async()=>{await api('/content','POST',{kind:'report',title:`运营日报 ${workDate.value}`,content:JSON.stringify(report.value,null,2)});message.value='日报已保存到文案与报告'})}
function pretty(value:any){return typeof value==='string'?value:JSON.stringify(value,null,2)}
watch(busy,value=>emit('busy',value))
// Every operations view uses the same snapshot. refreshKey handles navigation,
// explicit refreshes and scope changes without starting a second load watcher.
watch(()=>props.refreshKey,()=>{void act(load)})
watch(selectedShop,()=>{scriptForm.product_id=scriptProducts.value[0]?.sku_id || '';scriptForm.reference_id='';peerForm.asset_id='';assetGroup.value=selectedShop.value==='b'?'WOM-001':'MEN-001'})
function escapeModal(event:KeyboardEvent){if(event.key==='Escape'&&!busy.value){taskEdit.value=null;scriptEdit.value=null;closeMedia()}}
onMounted(()=>{assetGroup.value=selectedShop.value==='b'?'WOM-001':'MEN-001';window.addEventListener('keydown',escapeModal);void act(load)})
const poll=window.setInterval(async()=>{if(!busy.value&&alive&&jobs.value.some(j=>['queued','running'].includes(j.status))){try{await load()}catch{ /* manual refresh remains available */ }}},3000)
onUnmounted(()=>{alive=false;generation++;window.clearInterval(poll);window.removeEventListener('keydown',escapeModal);closeMedia();emit('busy',false)})
</script>

<template>
  <div class="ops-workspace">
    <div v-if="message" :class="['ops-message',{error:failed}]" role="status">{{message}}</div>
    <div class="ops-toolbar"><span v-if="summary">{{summary.by_shop.some((s:any)=>s.latest_date)?'已导入报表':'数据待导入'}} · {{summary.is_demo?'合成测试数据':'店铺数据'}} · 截止 {{summary.as_of?.slice(0,16).replace('T',' ')}}</span><button class="secondary" :disabled="busy" @click="act(load)">{{busy?'处理中…':'刷新数据'}}</button></div>
    <section v-if="!loaded&&!failed" class="panel ops-loading" role="status" aria-live="polite"><p>正在读取店铺运营数据…</p></section>
    <section v-if="loaded&&!summary?.by_shop.some((s:any)=>s.latest_date)" class="panel ops-empty">
      <h3>先加载运营数据</h3><p>可以一键加载已生成的两店测试数据，或在“数据导入”中校验自己的 CSV、JSON、JSONL 或 Excel 报表。</p><button class="primary" :disabled="busy" @click="demo">加载完整测试数据</button>
    </section>

    <template v-if="view==='today' && loaded">
      <div class="ops-controls ops-field-controls">
        <label for="ops-work-date">工作日期</label>
        <div class="ops-control-row">
          <input id="ops-work-date" v-model="workDate" type="date" :disabled="busy" required @change="act(load)"/>
          <button class="primary" :disabled="busy" @click="generateTasks">生成今日SOP</button>
          <button class="secondary" :disabled="busy" @click="emit('ask',`根据已导入报表和${workDate}的任务，说明今天运营优先做什么，并指出数据缺口。`)">让 Agent 安排重点</button>
        </div>
      </div>
      <div class="metric-grid"><article v-for="state in ['pending','in_progress','blocked','completed']" :key="state"><small>{{statuses[state]}}</small><strong>{{taskCounts[state]}}</strong><span>{{workDate}}</span></article></div>
      <section class="panel"><div class="section-label"><h3>每日工作 <span>{{activeTasks.length}}项 · 重复生成保留已有进度</span></h3></div>
        <div v-for="t in activeTasks" :key="t.id" class="ops-task"><div><span class="status">{{statuses[t.status]}}</span><strong>{{t.title}}</strong><p>{{name(t.shop_id)}} · {{t.owner}} · 截止 {{dueTime(t.due_at)}} · v{{t.version}}</p><small v-if="t.note">{{t.note}}</small><small v-if="t.evidence">完成证据：{{t.evidence}}</small></div><button class="secondary" :disabled="busy" @click="taskEdit={...t}">跟进任务</button></div>
        <p v-if="!activeTasks.length" class="muted">当天暂无任务，可生成SOP。</p>
      </section>
      <div class="two-columns"><section class="panel"><h3>运营日报</h3><p>已完成 {{report?.completed.length||0}} 项 · 遗留 {{report?.overdue.length||0}} 项</p><button class="secondary" :disabled="busy" @click="saveReport">保存运营日报</button></section>
        <section class="panel"><h3>每日自动生成</h3><p class="muted">服务运行期间按上海时区执行；已有任务不覆盖。</p><div v-for="s in schedules" :key="s.shop_id" class="ops-line"><label><input v-model="s.enabled" type="checkbox"/>{{name(s.shop_id)}}</label><label>时间<input v-model.number="s.hour" type="number" min="0" max="23"/>时</label><button class="text-button" :disabled="busy" @click="saveSchedule(s)">保存计划</button><small v-if="s.error">{{s.error}}</small><small v-else>上次执行：{{s.last_date||'尚未执行'}}</small></div></section></div>
    </template>

    <template v-if="view==='ops-analysis' && summary">
      <div class="ops-controls ops-field-controls">
        <label for="ops-period">统计周期</label>
        <div class="ops-control-row">
          <select id="ops-period" v-model="days" :disabled="busy" @change="act(load)"><option :value="7">最近7个完整自然日</option><option :value="30">最近30个完整自然日</option><option :value="60">最近60个完整自然日</option></select>
          <span>{{summary.period.start}} 至 {{summary.period.end_exclusive}}（不含）</span>
        </div>
      </div>
      <div v-if="!summary.complete" class="ops-message error">部分店铺或日期缺少数据。下方显示已覆盖日期的小计，需先补齐报表再比较。</div>
      <div class="metric-grid"><article><small>支付成交额</small><strong>{{money(summary.total.paid_fen)}}</strong><span>按支付时间</span></article><article><small>期间退款</small><strong>{{money(summary.total.refund_fen)}}</strong><span>按退款完成时间</span></article><article><small>净收款</small><strong>{{money(summary.total.net_receipts_fen)}}</strong><span>不等于利润</span></article><article><small>买家日转化率</small><strong>{{percent(summary.total.buyer_day_conversion)}}</strong><span>分子、分母分别汇总</span></article></div>
      <section class="panel"><div class="table-wrap"><table><thead><tr><th>店铺</th><th>支付成交额</th><th>环比</th><th>退款</th><th>父订单 / 子订单</th><th>数据覆盖</th></tr></thead><tbody><tr v-for="s in summary.by_shop" :key="s.shop_id"><td>{{s.shop_name}}</td><td>{{money(s.paid_fen)}}</td><td>{{s.complete && s.growth_percent!=null?s.growth_percent+'%':'暂无可比数据'}}</td><td>{{money(s.refund_fen)}}</td><td>{{s.paid_parent_orders??'未知'}} / {{s.paid_sub_orders??'未知'}}</td><td>{{s.complete?'完整':`缺${s.missing_dates.length}天`}}<details v-if="s.missing_dates.length"><summary>缺失日期</summary>{{s.missing_dates.join('、')}}</details></td></tr></tbody></table></div></section>
      <div class="two-columns"><section class="panel"><h3>低库存核查</h3><div v-for="r in summary.low_stock" :key="[r.shop_id,r.sku_id].join(':')" class="ops-line">{{name(r.shop_id)}} · {{r.sku_id}}<p>可售 {{r.available_stock}}，近7日销售 {{r.sold_units_7d}} 件</p></div></section><section class="panel"><h3>发货与退款</h3><p>超过承诺时间未发货：{{summary.overdue_shipment_count}} 单</p><details><summary>查看订单证据</summary><p v-for="o in summary.overdue_shipments" :key="[o.shop_id,o.sub_order_id].join(':')">{{name(o.shop_id)}} · {{o.sub_order_id}}</p></details><div v-for="(amount,reason) in summary.refund_by_reason" :key="reason" class="ops-line">{{reason}}：{{money(Number(amount))}}</div><small>明细对账：支付 {{summary.reconciliation.payments===null?'资料不足':summary.reconciliation.payments?'一致':'有差异'}} · 退款 {{summary.reconciliation.refunds===null?'资料不足':summary.reconciliation.refunds?'一致':'有差异'}}</small></section></div>
      <button class="primary" :disabled="busy" @click="emit('ask',`用ops_overview分析最近${days}天所有授权店铺，区分事实、待验证原因和具体行动。`)">生成全店经营诊断</button>
    </template>


    <template v-if="view==='ops-imports' && loaded">
      <section class="panel"><h3>测试数据</h3><p>加载两家店的订单、日报、库存、视频、SOP、素材与脚本，随后可直接分析和剪辑。已回填的任务保留。</p><button class="primary" :disabled="busy" @click="demo">加载完整测试数据</button></section>
      <ReportImporter :key="shopId || 'all'" :kinds="kinds" :shops="shops" :shop-id="shopId" :disabled="busy" @busy="busy=$event" @imported="load"/>
      <section class="panel"><h3>导入记录</h3><div class="table-wrap"><table><thead><tr><th>文件</th><th>类型</th><th>行数</th><th>状态</th></tr></thead><tbody><tr v-for="b in imports" :key="b.id"><td>{{b.filename}}</td><td>{{kindNames[b.kind]||b.kind}}</td><td>{{b.rows}}</td><td>{{b.status==='committed'?'已导入':'已校验'}} · {{b.errors}}错误</td></tr></tbody></table></div></section>
    </template>

    <template v-if="view==='ops-videos' && loaded">
      <section class="panel"><h3>账号短视频表现</h3><div class="table-wrap ops-video-metrics"><table><thead><tr><th>店铺 / 视频</th><th>播放量</th><th>商品点击率</th><th>完播率</th><th>归因支付</th><th>诊断</th></tr></thead><tbody><tr v-for="v in videos" :key="[v.shop_id,v.video_id].join(':')"><td>{{name(v.shop_id)}} · {{v.title}}<small>{{v.video_id}} · {{v.metric_grain==='daily_increment'?'日增量':v.metric_grain==='cumulative'?'累计快照':'口径未提供'}}</small></td><td>{{v.views?.toLocaleString()??'暂无数据'}}</td><td>{{percent(v.click_rate)}}</td><td>{{percent(v.completion_rate)}}</td><td>{{money(v.attributed_paid_fen)}}</td><td>{{v.warning||'可继续对比同类内容'}}</td></tr></tbody></table></div></section>
      <div class="two-columns"><section class="panel ops-form"><h3>素材库</h3><label>店铺<select v-model="selectedShop" :disabled="busy"><option v-for="s in shops" :key="s.id" :value="s.id">{{s.name}}</option></select></label><label>素材所属款式<select v-model="assetGroup"><option value="">同行参考 / 不绑定商品</option><option v-for="p in products.filter((p,i,all)=>p.shop_id===selectedShop&&all.findIndex(x=>x.shop_id===p.shop_id&&x.group_id===p.group_id)===i)" :key="p.group_id" :value="p.group_id">{{p.title}} · {{p.group_id}}</option></select></label><label>上传视频（最多64MB、180秒）<input type="file" accept="video/mp4,video/webm,video/quicktime" :disabled="busy" @change="upload"/></label><div v-for="a in scopedAssets" :key="a.id" class="ops-line"><strong>{{a.title}}</strong><p>{{a.group_id||'参考素材'}} · {{a.metadata.duration_seconds.toFixed(2)}}秒 · {{a.metadata.visual||'本地视频'}}</p><button class="text-button" :disabled="busy" @click="openMedia(a.media_url,a.title)">预览素材</button></div></section>
        <form class="panel ops-form" @submit.prevent="createPeer"><h3>添加同行参考</h3><label>标题<input v-model="peerForm.title" required maxlength="180"/></label><label>参考素材<select v-model="peerForm.asset_id"><option value="">仅使用提供的转写文本</option><option v-for="a in scopedAssets" :key="a.id" :value="a.id">{{a.title}}</option></select></label><label>字幕或转写<textarea v-model="peerForm.transcript" required minlength="5" maxlength="12000" placeholder="可包含0-4秒、4-8秒等分镜时间"/></label><label>来源说明<input v-model="peerForm.source" maxlength="500"/></label><button class="primary" :disabled="busy">保存同行参考</button><p class="footnote">按提供的转写、媒体时长和场景切换拆解；不会推断同行后台成交。</p></form></div>
      <section class="panel"><h3>同行拆解</h3><article v-for="p in peers" :key="[p.shop_id,p.reference_id].join(':')" class="ops-peer"><strong>{{name(p.shop_id)}} · {{p.title}}</strong><p>{{p.transcript}}</p><p v-if="p.measurements">实测时长 {{p.measurements.duration_seconds?.toFixed(2)}}秒，场景切换 {{p.measurements.scene_change_seconds?.join('、')||'未检测到'}}。</p><button class="secondary" :disabled="busy" @click="analyzePeer(p.reference_id)">生成拆解报告</button><pre v-if="p.analysis">{{pretty(p.analysis)}}</pre><small v-if="p.limitation">{{p.limitation}}</small></article></section>
      <form class="panel ops-form" @submit.prevent="generateScript"><h3>生成可剪辑脚本</h3><label>店铺<select v-model="selectedShop" :disabled="busy"><option v-for="s in shops" :key="s.id" :value="s.id">{{s.name}}</option></select></label><label>商品（需有对应款式素材）<select v-model="scriptForm.product_id" required><option v-for="p in scriptProducts" :key="p.sku_id" :value="p.sku_id">{{p.title}}</option></select></label><label>同行参考<select v-model="scriptForm.reference_id"><option value="">不使用同行参考</option><option v-for="p in shopPeers" :key="p.reference_id" :value="p.reference_id">{{p.title}}</option></select></label><label>创作要求<textarea v-model="scriptForm.brief" required maxlength="2000"/></label><button class="primary" :disabled="busy||!scriptForm.product_id">生成分镜与字幕</button></form>
      <section class="panel"><h3>脚本与版本</h3><article v-for="s in scripts" :key="s.id" class="ops-peer"><strong>{{name(s.shop_id)}} · {{s.title}} · v{{s.version}}</strong><p>{{s.payload.duration_seconds}}秒 · {{s.payload.generation}}</p><ol><li v-for="(part,i) in s.payload.segments" :key="i">{{part.asset_id}}，截取 {{part.in_seconds}}～{{part.out_seconds}}秒：{{part.caption}}</li></ol><div class="ops-controls"><button class="secondary" :disabled="busy" @click="editScript(s)">编辑脚本</button><button class="primary" :disabled="busy" @click="renderScript(s)">开始自动剪辑</button></div><ScriptReview :script="s" :disabled="busy" @busy="busy=$event" @saved="load"/></article></section>
      <section class="panel"><h3>剪辑任务</h3><article v-for="j in jobs" :key="j.id" class="ops-peer"><strong>{{name(j.shop_id)}} · {{statuses[j.status]}} · 第{{j.attempts}}次执行</strong><p>{{j.error||j.result?.note||'任务已持久化，可刷新查看进度。'}}</p><small>{{j.id}}</small><div class="ops-controls" v-if="j.status==='succeeded'"><button class="primary" :disabled="busy" @click="openMedia(j.result.video_url,'成片预览')">预览成片</button><button class="secondary" :disabled="busy" @click="download(j.result.video_url,`${j.id}.mp4`)">下载成片</button><button class="text-button" :disabled="busy" @click="download(j.result.subtitles_url,`${j.id}.srt`)">字幕</button><button class="text-button" :disabled="busy" @click="download(j.result.cover_url,`${j.id}.jpg`)">封面</button></div><button v-if="j.status==='failed'" class="secondary" :disabled="busy" @click="retryJob(j.id)">重试剪辑</button></article><p v-if="!jobs.length" class="muted">尚无剪辑任务</p></section>
    </template>


    <div v-if="taskEdit" class="modal-backdrop" @click.self="taskEdit=null"><form class="modal ops-form" @submit.prevent="saveTask"><button type="button" class="modal-close icon-button" aria-label="关闭任务" @click="taskEdit=null">×</button><h2>跟进任务</h2><p>{{taskEdit.title}}</p><label>负责人<input v-model="taskEdit.owner" required maxlength="100"/></label><label>任务状态<select v-model="taskEdit.status"><option v-for="s in ['pending','in_progress','blocked','review','completed','cancelled']" :key="s" :value="s">{{statuses[s]}}</option></select></label><label>进展或阻塞原因<textarea v-model="taskEdit.note" maxlength="5000"/></label><label>完成证据<textarea v-model="taskEdit.evidence" maxlength="5000" placeholder="完成时填写实际产出、记录或核查依据"/></label><details><summary>历史记录</summary><pre>{{pretty(taskEdit.history)}}</pre></details><button class="primary" :disabled="busy">保存任务进度</button></form></div>
    <div v-if="scriptEdit" class="modal-backdrop" @click.self="scriptEdit=null"><form class="modal large ops-form" @submit.prevent="saveScript"><button type="button" class="modal-close icon-button" aria-label="关闭脚本" @click="scriptEdit=null">×</button><h2>编辑脚本</h2><label>标题<input v-model="scriptEdit.title" required maxlength="180"/></label><fieldset v-for="(segment,index) in scriptEdit.segments" :key="index"><legend>第 {{Number(index)+1}} 段</legend><label class="segment-lock"><input v-model="segment.locked" type="checkbox"/>锁定本段字幕，定向改写时保留</label><label>素材<select v-model="segment.asset_id"><option v-for="a in assets.filter(a=>a.shop_id===scriptEdit.shop_id)" :key="a.id" :value="a.id">{{a.title}} · {{a.metadata.duration_seconds}}秒</option></select></label><div class="two-columns"><label>开始秒数<input v-model.number="segment.in_seconds" type="number" min="0" step="0.1" required/></label><label>结束秒数<input v-model.number="segment.out_seconds" type="number" min="0.1" step="0.1" required/></label></div><label>字幕<textarea v-model="segment.caption" required maxlength="100"/></label></fieldset><p class="footnote">截取范围须在素材时长内，保存后可重新剪辑。</p><button class="primary" :disabled="busy">保存新版本</button></form></div>
    <div v-if="mediaUrl" class="modal-backdrop" @click.self="closeMedia"><section class="modal ops-video-modal"><button type="button" class="modal-close icon-button" aria-label="关闭视频" @click="closeMedia">×</button><h2>{{mediaTitle}}</h2><video :src="mediaUrl" controls playsinline autoplay/></section></div>
  </div>
</template>

<style scoped>
.ops-controls.ops-field-controls{flex-direction:column;align-items:flex-start;gap:8px}
.ops-control-row{display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;width:100%;min-width:0}
.ops-control-row>input,.ops-control-row>select,.ops-control-row>button{min-height:40px;height:auto;font-size:12px;line-height:20px;padding-block:9px}
.ops-control-row>input{min-width:160px}
.ops-control-row>select{min-width:218px;max-width:100%}
.ops-control-row>span{line-height:1.6;font-variant-numeric:tabular-nums}
.ops-toolbar{min-height:40px}
.ops-loading{min-height:240px;display:grid;place-items:center}
.ops-toolbar,.ops-controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:20px}.ops-toolbar{justify-content:space-between;font-size:11px;color:var(--muted)}.ops-message{padding:13px 16px;margin-bottom:18px;border-radius:8px;background:#edf4eb;font-size:12px;line-height:1.8}.ops-message.error{background:#fff0e5;color:#925a39}.ops-workspace .ops-form label{font-size:12px;margin:12px 0;gap:7px}.ops-form input,.ops-form select,.ops-form textarea{width:100%;font-size:12px}.ops-controls label{margin:0;min-width:160px}.ops-controls span{font-size:11px}.ops-task{display:flex;align-items:flex-start;justify-content:space-between;gap:15px;padding:17px 0;border-bottom:1px solid var(--line)}.ops-task>div{min-width:0;flex:1}.ops-task strong{display:inline;margin-left:9px;font-size:13px}.ops-task p,.ops-line p,.ops-peer p,.panel>p{font-size:11px;line-height:1.9;color:var(--muted);margin:9px 0}.ops-task small,.ops-line small,td small{display:block;font-size:10px;line-height:1.8;overflow-wrap:anywhere}.ops-line{font-size:12px;line-height:1.9;padding:12px 0;border-bottom:1px solid var(--line)}.ops-line label{display:inline-flex;align-items:center;gap:8px;margin-right:10px}.ops-line input[type=checkbox]{width:auto}.ops-line input[type=number]{width:55px}.ops-peer{padding:20px 0;border-bottom:1px solid var(--line)}.ops-peer>strong{font-size:13px}.ops-peer li{font-size:11px;line-height:2}.ops-workspace pre{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.9 system-ui;color:#52685d;max-height:450px;overflow:auto;background:#f5f7f2;padding:13px;border-radius:7px}.ops-workspace details{font-size:11px;line-height:1.8;margin:12px 0}.ops-workspace td{font-size:10px;min-width:90px}.ops-workspace td:first-child{min-width:180px}.ops-workspace .metric-grid strong{font-size:clamp(16px,11cqi,22px)}.ops-video-modal video{width:100%;max-height:65vh;background:#111}.ops-form .code-input{font-family:monospace}.ops-empty h3{margin-bottom:12px}@media(max-width:650px){.ops-toolbar{align-items:flex-start}.ops-task{flex-wrap:wrap}.ops-task>.secondary{margin-left:auto}.ops-controls{gap:8px}.ops-workspace .metric-grid strong{font-size:18px}.ops-workspace .panel{padding:16px}}
.segment-lock{display:flex!important;flex-direction:row;align-items:center}.segment-lock input{width:auto!important}.ops-video-metrics{max-height:560px;overflow:auto}.ops-form fieldset{border:1px solid var(--line);border-radius:8px;padding:12px;margin:14px 0}.ops-form legend{font-size:12px;padding:0 5px}
</style>





