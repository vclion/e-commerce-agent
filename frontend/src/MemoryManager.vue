<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from './api'
const props=defineProps<{workspace:string;shopId:string|null}>()
const emit=defineEmits<{close:[];changed:[]}>()
const items=ref<any[]>([]), selected=ref<any>(null), busy=ref(false), error=ref(''), message=ref('')
const preferences=reactive({read_enabled:true,auto_write:true,context_index_enabled:true,reflection_enabled:true})
const background=reactive({pending:false,failed:0})
const form=reactive({kind:'semantic',scope:props.shopId?'personal_shop':'personal',subject:'report',predicate:'preference',title:'',content:'',valid_from:'',expires_at:''})
const labels:Record<string,string>={semantic:'偏好与约定',decision:'已确认决策',episodic:'历史事件',procedural:'流程经验',active:'生效',candidate:'待确认',disabled:'已停用',cancelled:'已取消',expired:'已过期',personal:'我的通用偏好',personal_shop:'当前店铺（仅我）',workspace:'当前栏目（仅我）',shop_shared:'店铺共享约定'}
async function act(fn:()=>Promise<void>){if(busy.value)return;busy.value=true;error.value='';message.value='';try{await fn()}catch(e){error.value=e instanceof Error?e.message:'操作失败'}finally{busy.value=false}}
async function load(){const [list,prefs]=await Promise.all([api(`/memory/items?workspace=${props.workspace}`),api('/memory/settings')]);items.value=list.items;Object.assign(preferences,{read_enabled:prefs.read_enabled,auto_write:prefs.auto_write,context_index_enabled:prefs.context_index_enabled,reflection_enabled:prefs.reflection_enabled});Object.assign(background,{pending:prefs.background_pending,failed:prefs.background_failed||0})}
async function inspect(item:any){await act(async()=>{selected.value=await api(`/memory/items/${item.id}`);edit()})}
function times(){return {valid_from:form.valid_from?new Date(form.valid_from).toISOString():null,expires_at:form.expires_at?new Date(form.expires_at).toISOString():null}}
function localTime(value:string|null){if(!value)return '';const date=new Date(value);return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16)}
function reset(){selected.value=null;form.title='';form.content='';form.valid_from='';form.expires_at=''}
async function save(){await act(async()=>{if(selected.value){await api(`/memory/items/${selected.value.id}`,'PATCH',{expected_revision:selected.value.revision,content:form.content,title:form.title,status:'active',...times()})}else{await api('/memory/items','POST',{...form,...times(),workspace:props.workspace})}reset();await load();message.value='记忆已保存';emit('changed')})}
function edit(){if(!selected.value)return;form.title=selected.value.title;form.content=selected.value.scheduled_content||selected.value.content;form.valid_from=selected.value.scheduled_content?localTime(selected.value.valid_from):'';form.expires_at=localTime(selected.value.expires_at)}
async function status(item:any,value:string){await act(async()=>{await api(`/memory/items/${item.id}`,'PATCH',{expected_revision:item.revision,content:item.scheduled_content||item.content,title:item.title,status:value,valid_from:item.scheduled_content?item.valid_from:null,expires_at:item.expires_at});reset();await load();emit('changed')})}
async function publish(item:any){await act(async()=>{await api(`/memory/items/${item.id}/publish`,'POST',{expected_revision:item.revision});await load();message.value='已发布到店铺共享记忆';emit('changed')})}
async function retry(){await act(async()=>{await api('/memory/jobs/retry','POST',{});await load();message.value='已安排重试，整理完成前不会显示为已保存'})}
async function forget(item:any){await act(async()=>{const result=await api('/memory/forget','POST',{item_ids:[item.id]});selected.value=null;form.content='';form.title='';await load();message.value=result.notice;emit('changed')})}
async function savePreferences(){await act(async()=>{await api('/memory/settings','PATCH',preferences);message.value='设置已保存';emit('changed')})}
onMounted(()=>act(load))
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')"><section class="modal large memory-manager" role="dialog" aria-modal="true" aria-label="记忆管理">
    <button class="modal-close icon-button" aria-label="关闭记忆管理" @click="emit('close')">×</button>
    <h2>记忆管理</h2><p class="muted">管理我的偏好、店铺约定和历史经验。原始聊天按原栏目保留，记忆不会改变业务权限。</p>
    <div class="memory-settings"><label><input type="checkbox" v-model="preferences.read_enabled"/>回答时使用长期记忆</label><label><input type="checkbox" v-model="preferences.auto_write"/>长期记忆自动写入</label><label><input type="checkbox" v-model="preferences.context_index_enabled"/>上下文索引整理</label><label><input type="checkbox" v-model="preferences.reflection_enabled" :disabled="!preferences.auto_write"/>整理经验候选</label><button class="secondary" :disabled="busy" @click="savePreferences">保存设置</button></div>
    <p class="muted">自动写入用于提取长期记忆候选和记录已执行事件；索引整理用于回查当前会话的较早对话。关闭后保留已有内容，最近 3 轮加载与超限临时压缩继续生效。</p>
    <p v-if="background.pending" class="muted" role="status">部分对话正在后台整理，可稍后刷新查看。</p>
    <p v-if="background.failed" class="muted" role="status">有 {{background.failed}} 项整理尚未完成。<button class="text-button" :disabled="busy" @click="retry">重新整理</button></p>
    <p v-if="error" class="error" role="alert">{{error}}</p><p v-if="message" class="memory-notice" role="status">{{message}}</p>
    <div class="memory-layout"><div>
      <div class="section-label"><h3>当前可用记忆</h3><button class="text-button" :disabled="busy" @click="act(load)">刷新</button></div>
      <p v-if="!items.length" class="empty compact">还没有记忆，可以保存一个报告或文案偏好。</p>
      <article v-for="item in items" :key="item.id" class="memory-card">
        <div><strong>{{item.title}}</strong><span class="status neutral">{{labels[item.status]||item.status}}</span></div>
        <small>{{labels[item.kind]}} · {{labels[item.scope]}} {{item.workspace||''}}</small><p>{{item.content||item.scheduled_content}}</p>
        <div class="memory-actions"><button class="text-button" :disabled="busy" @click="inspect(item)">{{item.can_manage?'查看来源 / 纠正':'查看来源'}}</button><button v-if="item.can_manage&&item.status==='candidate'" class="text-button" :disabled="busy" @click="status(item,'active')">确认采用</button><button v-if="item.can_manage&&item.status==='active'" class="text-button" :disabled="busy" @click="status(item,'disabled')">停用</button><button v-if="item.can_publish" class="text-button" :disabled="busy" @click="publish(item)">发布到店铺共享</button><button v-if="item.can_manage" class="text-button" :disabled="busy" @click="forget(item)">忘记</button></div>
      </article>
    </div><form class="memory-form" @submit.prevent="save">
      <h3>{{selected?'纠正记忆':'保存一条记忆'}}</h3>
      <template v-if="!selected"><label>类型<select v-model="form.kind"><option value="semantic">偏好与约定</option><option value="decision">已确认决策</option><option value="episodic">历史事件</option></select></label><label>使用范围<select v-model="form.scope"><option value="personal">我的通用偏好</option><option v-if="shopId" value="personal_shop">当前店铺（仅我）</option><option value="workspace">当前栏目（仅我）</option></select></label><div class="memory-key"><label>主题<input v-model="form.subject" required maxlength="180"/></label><label>属性<input v-model="form.predicate" required maxlength="100"/></label></div></template>
      <label>标题<input v-model="form.title" :readonly="selected&&!selected.can_manage" required maxlength="180" placeholder="例如：报告排序偏好"/></label><label>内容<textarea v-model="form.content" :readonly="selected&&!selected.can_manage" required maxlength="4000" rows="5" placeholder="例如：报告先列退款和履约异常，再列建议。"/></label>
      <template v-if="!selected||selected.can_manage"><details><summary>生效时间与有效期（可选）</summary><label>生效时间<input type="datetime-local" v-model="form.valid_from"/></label><label>失效时间<input type="datetime-local" v-model="form.expires_at"/></label></details><button class="primary" :disabled="busy||!form.content.trim()||!form.title.trim()">{{busy?'处理中…':'保存记忆'}}</button></template>
      <template v-if="selected"><button type="button" class="text-button" @click="reset">返回新增</button><details open><summary>来源与版本</summary><p v-for="(ev,i) in selected.evidence" :key="i" class="muted">{{ev.source_role==='user'?'用户陈述':ev.source_role==='tool'?'工具结果':'归纳候选'}} · {{ev.note}} <span v-if="ev.quote">：{{ev.quote}}</span></p><p v-for="version in selected.versions" :key="version.id" class="muted">版本 {{version.revision}} · {{version.reason}}<br/>{{version.content}}</p></details></template>
    </form></div>
  </section></div>
</template>

<style scoped>
.memory-manager{width:min(1000px,94vw);max-height:90vh;overflow:auto}.memory-settings{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid #e4e9e3}.memory-settings label{flex-direction:row;align-items:center;margin:0;font-size:12px}.memory-settings input{width:16px;height:16px}.memory-layout{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr);gap:24px;margin-top:20px}.memory-card{padding:14px;border:1px solid #e4e9e3;border-radius:8px;margin-bottom:12px}.memory-card>div:first-child{display:flex;justify-content:space-between;gap:10px}.memory-card p{font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;margin:10px 0}.memory-card strong{font-size:13px}.memory-actions{display:flex;gap:12px;flex-wrap:wrap}.memory-actions button{font-size:11px}.memory-form label{margin:12px 0}.memory-form details{margin-top:18px}.memory-form details p{overflow-wrap:anywhere}.memory-key{display:flex;gap:10px}.memory-key label{min-width:0;flex:1}.memory-notice{color:#236450;font-size:12px;line-height:1.7}@media(max-width:700px){.memory-layout{grid-template-columns:1fr}.memory-settings{align-items:flex-start;flex-direction:column}.memory-manager{padding:22px 16px}.memory-key{flex-direction:column;gap:0}}
</style>
