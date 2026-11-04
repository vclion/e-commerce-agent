<script setup lang="ts">
import {computed, onMounted, ref, watch} from 'vue'
import {api} from './api'

const props=defineProps<{kinds:string[]; shops:{id:string;name:string}[]; shopId:string|null; disabled:boolean}>()
const emit=defineEmits<{busy:[value:boolean]; imported:[]}>()
const kind=ref('daily_reports'), file=ref<File|null>(null), source=ref<any>(null)
const inspection=ref<any>(null), preview=ref<any>(null), mapping=ref<any>(null), mapped=ref(false)
const working=ref(false), message=ref(''), failed=ref(false), templates=ref<any[]>([]), templateName=ref('')
const valueMapDrafts=ref<Record<string,string>>({})
const sourceReady=ref(false)
const names:Record<string,string>={shops:'店铺资料',staff:'人员资料',products:'商品资料',orders:'订单明细',refunds:'售后退款',daily_reports:'经营日报',inventory:'库存',videos:'视频清单',video_daily:'视频日指标',sop_templates:'SOP模板',tasks:'运营任务',peer_references:'同行参考'}
const disabled=computed(()=>props.disabled||working.value)
const shops=computed(()=>props.shopId?props.shops.filter(s=>s.id===props.shopId):props.shops)
const matchingTemplates=computed(()=>templates.value.filter(t=>t.kind===kind.value&&shops.value.some(s=>s.id===t.mapping.shop_id)))
async function run(fn:()=>Promise<void>){
  if(disabled.value)return
  working.value=true;emit('busy',true);message.value='';failed.value=false
  try{await fn()}catch(e){failed.value=true;message.value=e instanceof Error?e.message:'操作失败'}
  finally{working.value=false;emit('busy',false)}
}
async function readFile(){
  if(!file.value)throw new Error('请选择报表文件')
  if(file.value.size>16*1024*1024)throw new Error('报表最大16MB，请拆分后上传')
  if(file.value.name.toLowerCase().endsWith('.xlsx')){
    const base64=await new Promise<string>((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]!);r.onerror=()=>reject(new Error('文件读取失败'));r.readAsDataURL(file.value!)})
    return {filename:file.value.name,content_base64:base64}
  }
  return {filename:file.value.name,text:await file.value.text()}
}
async function inspectSource(sheet?:string){
  sourceReady.value=false
  preview.value=null
  source.value=source.value||await readFile()
  const input=source.value, sourceKind=kind.value
  const result=await api('/ops/imports/inspect','POST',{...input,kind:sourceKind,sheet})
  if(source.value!==input||kind.value!==sourceKind)return
  inspection.value=result
  mapping.value={...inspection.value.mapping,shop_id:shops.value[0]?.id||null}
  valueMapDrafts.value={}
  const required=inspection.value.fields.filter((f:any)=>!f.nullable)
  mapped.value=!!input.content_base64||required.some((f:any)=>!inspection.value.headers.includes(f.key))
  sourceReady.value=true
}
async function selectFile(event:Event){
  if(disabled.value)return
  file.value=(event.target as HTMLInputElement).files?.[0]||null
  source.value=null;inspection.value=null;preview.value=null;mapping.value=null
  if(file.value)await run(()=>inspectSource())
}
async function validate(){await run(async()=>{
  if(!source.value){await inspectSource()}
  preview.value=await api('/ops/imports/preview','POST',{...source.value,kind:kind.value,
    sheet:inspection.value?.sheet,mapping:mapped.value?currentMapping():undefined})
})}
async function commit(){await run(async()=>{
  const result=await api(`/ops/imports/${preview.value.id}/commit`,'POST',{})
  preview.value=null;message.value=result.duplicate_batch?'该批次已导入，没有重复计数。':`已写入 ${result.imported} 条记录。`
  emit('imported')
})}
async function downloadTemplate(){await run(async()=>{
  const result=await api(`/ops/templates/${kind.value}`)
  const url=URL.createObjectURL(new Blob([result.text],{type:'text/csv;charset=utf-8'}))
  const a=document.createElement('a');a.href=url;a.download=result.filename;a.click();URL.revokeObjectURL(url)
})}
async function saveMapping(){await run(async()=>{
  if(!templateName.value.trim())throw new Error('请填写映射模板名称')
  await api('/ops/import-mappings','POST',{name:templateName.value.trim(),kind:kind.value,mapping:currentMapping()})
  templates.value=(await api('/ops/import-mappings')).items;message.value='字段映射模板已保存，下次同类报表可复用。'
})}
function applyTemplate(event:Event){
  const selected=(event.target as HTMLSelectElement).value
  if(selected==='')return
  const item=matchingTemplates.value[Number(selected)]
  if(!item)return
  mapping.value=JSON.parse(JSON.stringify(item.mapping));mapped.value=true;preview.value=null
  valueMapDrafts.value={}
  templateName.value=item.name
}
function chooseColumn(key:string,event:Event){
  const column=(event.target as HTMLSelectElement).value
  if(column)mapping.value.columns[key]=column
  else delete mapping.value.columns[key]
}
function setValueMap(key:string,event:Event){
  valueMapDrafts.value[key]=(event.target as HTMLTextAreaElement).value
  preview.value=null
}
function currentMapping(){
  const result=JSON.parse(JSON.stringify(mapping.value))
  for(const [key,text] of Object.entries(valueMapDrafts.value)){
    const entries:Record<string,string>={}
    for(const line of text.split('\n').filter(x=>x.trim())){
      const index=line.indexOf('='), source=line.slice(0,index).trim(), target=line.slice(index+1).trim()
      if(index<1||!source||!target)throw new Error('值映射请逐行填写：原始值=标准值')
      if(source in entries)throw new Error(`值映射中「${source}」重复，请保留一项`)
      entries[source]=target
    }
    result.value_maps[key]=entries
  }
  return result
}
function pretty(value:any){return JSON.stringify(value,null,2)}
watch(mapping,()=>{preview.value=null},{deep:true})
watch(mapped,()=>{preview.value=null})
watch(kind,()=>{preview.value=null;inspection.value=null;mapping.value=null;if(file.value)void run(()=>inspectSource())})
onMounted(async()=>{try{templates.value=(await api('/ops/import-mappings')).items}catch{/* Saving a template will report any connection error. */}})
</script>

<template>
  <section class="panel ops-form report-importer">
    <h3>导入经营报表</h3>
    <p>支持 CSV、JSON、JSONL 和 Excel（.xlsx）。中文表头可映射为标准字段；校验通过后再确认入库。</p>
    <p v-if="message" :class="['import-message',{error:failed}]" role="status">{{message}}</p>
    <label>报表类型<select v-model="kind" :disabled="disabled"><option v-for="k in kinds" :key="k" :value="k">{{names[k]||k}}</option></select></label>
    <label>报表文件<input type="file" accept=".csv,.json,.jsonl,.xlsx" :disabled="disabled" @change="selectFile"/></label>
    <div v-if="inspection">
      <label v-if="inspection.sheets.length">工作表<select aria-label="工作表" :value="inspection.sheet" :disabled="disabled" @change="run(()=>inspectSource(($event.target as HTMLSelectElement).value))"><option v-for="sheet in inspection.sheets" :key="sheet">{{sheet}}</option></select></label>
      <p>已读取 {{inspection.row_count}} 行 · {{inspection.headers.length}} 列</p>
      <label class="inline-check"><input v-model="mapped" type="checkbox" :disabled="disabled||!!source.content_base64"/>使用字段映射与格式转换</label>
      <template v-if="mapped&&mapping">
        <p v-if="inspection.ambiguous.length">以下字段有多个候选，请手动选择：{{inspection.ambiguous.join('、')}}</p>
        <div class="mapping-settings"><label>导入店铺<select v-model="mapping.shop_id" :disabled="disabled"><option v-for="s in shops" :key="s.id" :value="s.id">{{s.name}}</option></select></label><label>源时间所属时区<select v-model="mapping.timezone" :disabled="disabled"><option value="Asia/Shanghai">上海（UTC+8）</option><option value="UTC">UTC</option></select></label></div>
        <p>已有时区的时间保持原值；未带时区的时间按以上选择转换。源数据店铺与所选店铺必须一致。</p>
        <label v-if="matchingTemplates.length">复用映射模板<select aria-label="复用映射模板" :disabled="disabled" @change="applyTemplate"><option value="">选择已保存模板</option><option v-for="(t,i) in matchingTemplates" :key="i" :value="i">{{t.name}}</option></select></label>
        <div class="mapping-fields">
          <div v-for="field in inspection.fields" :key="field.key" class="mapping-field">
            <strong>{{field.label}} <small>{{field.nullable?'可空':'必填'}} · {{field.key}}</small></strong>
            <label>源列<select :aria-label="`${field.label}源列`" :value="mapping.columns[field.key]||''" :disabled="disabled" @change="chooseColumn(field.key,$event)"><option value="">不使用源列</option><option v-for="header in inspection.headers" :key="header" :value="header">{{header}}</option></select></label>
            <label v-if="!mapping.columns[field.key]&&field.key!=='shop_id'">固定值<input v-model="mapping.defaults[field.key]" :disabled="disabled" :aria-label="`${field.label}固定值`" :placeholder="field.key==='dataset_version'?'默认 manual-import-v1':field.key==='is_demo'?'默认 false':field.nullable?'留空即未知':'缺失时需补充，不自动填0'"/></label>
            <label v-if="field.key.endsWith('_fen')">金额单位<select v-model="mapping.money_units[field.key]" :aria-label="`${field.label}金额单位`" :disabled="disabled"><option value="yuan">元（入库转换为分）</option><option value="fen">分（保持原值）</option></select></label>
            <details v-if="field.values.length"><summary>状态/值转换：{{field.values.join('、')}}</summary><label>自定义值映射<textarea :aria-label="`${field.label}自定义值映射`" :disabled="disabled" :value="valueMapDrafts[field.key]??Object.entries(mapping.value_maps[field.key]||{}).map(([a,b])=>`${a}=${b}`).join('\n')" placeholder="每行填写：原始值=标准值" @input="setValueMap(field.key,$event)"/></label></details>
          </div>
        </div>
        <div class="mapping-settings"><label>映射模板名称<input v-model="templateName" maxlength="80" :disabled="disabled" placeholder="如 抖店订单导出-当前表头"/></label><button class="secondary" :disabled="disabled||!templateName.trim()" @click="saveMapping">保存映射模板</button></div>
        <details><summary>原始数据样例</summary><pre>{{pretty(inspection.sample)}}</pre></details>
      </template>
    </div>
    <div class="import-actions"><button class="primary" :disabled="disabled||!file||!sourceReady" @click="validate">{{working?'处理中…':'校验报表'}}</button><button class="secondary" :disabled="disabled" @click="downloadTemplate">下载字段示例</button></div>
    <div v-if="preview" class="import-preview"><h4>校验结果：有效 {{preview.valid_rows}} 行 · 重复 {{preview.duplicates}} 行 · 错误 {{preview.error_count}} 行</h4><p v-for="error in preview.errors" :key="`${error.row}-${error.message}`" class="error">第 {{error.row}} 行：{{error.message}}</p><details open><summary>入库数据预览（最多5行）</summary><pre>{{pretty(preview.preview)}}</pre></details><button class="primary" :disabled="disabled||preview.error_count>0||preview.valid_rows===0" @click="commit">确认导入</button></div>
  </section>
</template>

<style scoped>
.report-importer p{font-size:12px;line-height:1.8;color:var(--muted)}.report-importer label{display:flex;flex-direction:column;gap:7px;margin:12px 0;font-size:12px}.report-importer input,.report-importer select,.report-importer textarea{width:100%;min-width:0;font-size:12px}.mapping-settings{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.mapping-settings label{flex:1;min-width:160px}.mapping-settings button{margin-bottom:12px}.mapping-fields{max-height:440px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:0 14px}.mapping-field{padding:12px 0;border-bottom:1px solid var(--line);display:grid;grid-template-columns:minmax(130px,1fr) 1fr 1fr;gap:12px;align-items:center}.mapping-field strong{font-size:12px;overflow-wrap:anywhere}.mapping-field small{display:block;font-size:10px;color:var(--muted);font-weight:400;margin-top:6px}.mapping-field details{grid-column:1/-1;font-size:11px}.report-importer .inline-check{flex-direction:row;align-items:center}.inline-check input{width:auto}.import-actions{display:flex;gap:12px;margin-top:16px;flex-wrap:wrap}.import-preview{margin-top:20px}.report-importer .error{color:#98552f}.import-message{padding:12px;border-radius:8px;background:#edf4eb}.import-message.error{background:#fff0e5}.report-importer pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto;background:#f5f7f2;padding:12px;font-size:11px}.report-importer details{font-size:12px;margin:12px 0}@media(max-width:650px){.mapping-field{grid-template-columns:1fr}.mapping-settings{display:block}}
</style>
