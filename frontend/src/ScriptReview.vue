<script setup lang="ts">
import {computed, ref, watch} from 'vue'
import {api} from './api'
const props=defineProps<{script:any;disabled:boolean}>()
const emit=defineEmits<{busy:[value:boolean]; saved:[]}>()
const report=ref<any>(null), candidate=ref<any>(null), message=ref(''), working=ref(false)
const review=computed(()=>report.value||props.script.payload.review)
const canRewrite=computed(()=>review.value?.issues.some((i:any)=>!i.locked))
const categories:Record<string,string>={price:'价格',material:'面料',size:'尺码',promotion:'促销',performance:'性能'}
async function run(fn:()=>Promise<void>){
  if(working.value||props.disabled)return
  working.value=true;emit('busy',true);message.value=''
  try{await fn()}catch(e){message.value=e instanceof Error?e.message:'操作失败'}
  finally{working.value=false;emit('busy',false)}
}
async function check(){await run(async()=>{
  report.value=await api(`/ops/scripts/${props.script.id}/review`,'POST',{expected_version:props.script.version})
  candidate.value=null
})}
async function rewrite(){await run(async()=>{
  candidate.value=await api(`/ops/scripts/${props.script.id}/rewrite`,'POST',{expected_version:props.script.version})
})}
async function adopt(){await run(async()=>{
  const next=candidate.value
  await api(`/ops/scripts/${props.script.id}`,'PATCH',{expected_version:next.expected_version,
    title:next.title,product_id:props.script.payload.product_id,
    reference_id:props.script.payload.reference_id||null,segments:next.segments})
  candidate.value=null;emit('saved');message.value='已采用改写并保存新版本。'
})}
watch(()=>props.script.version,()=>{report.value=null;candidate.value=null})
</script>

<template>
  <div class="script-review">
    <p v-if="message" role="status">{{message}}</p>
    <div class="review-actions"><button class="secondary" :disabled="disabled||working" @click="check">检查文案事实</button><button v-if="canRewrite" class="secondary" :disabled="disabled||working" @click="rewrite">{{working?'处理中…':'定向改写一轮'}}</button><span v-if="review">{{review.issue_count}} 处表述待核对</span></div>
    <details v-if="review" :open="review.issue_count>0&&!candidate"><summary>文案检查依据</summary><p>商品：{{review.source_facts.title}} · 面料：{{review.source_facts.material||'未知'}} · 尺码：{{review.source_facts.size||'未知'}}</p><article v-for="(issue,i) in review.issues" :key="i"><strong>{{issue.segment_index<0?'标题':`第${issue.segment_index+1}段`}} · {{categories[issue.category]||issue.category}}<span v-if="issue.locked"> · 已锁定</span></strong><p>「{{issue.claim}}」</p><p>{{issue.evidence}}</p><p>建议：{{issue.suggestion}}</p></article><p>{{review.note}}</p></details>
    <section v-if="candidate" class="rewrite-preview"><h4>改写差异 · 尚未保存</h4><article v-for="change in candidate.changes" :key="change.segment_index"><strong>{{change.segment_index<0?'标题':`第${change.segment_index+1}段`}}</strong><p><del>{{change.before}}</del></p><p>{{change.after}}</p></article><p v-if="!candidate.changes.length">本轮未产生改动。</p><p>改写后仍有 {{candidate.review.issue_count}} 处表述待核对。</p><p v-for="(issue,i) in candidate.review.issues" :key="i">{{issue.segment_index<0?'标题':`第${issue.segment_index+1}段`}}：{{issue.claim}} — {{issue.suggestion}}</p><div class="review-actions"><button class="primary" :disabled="disabled||working||!candidate.changes.length" @click="adopt">采用并保存新版本</button><button class="secondary" :disabled="disabled||working" @click="candidate=null">放弃改写</button></div></section>
  </div>
</template>

<style scoped>
.script-review{margin-top:16px;font-size:12px}.review-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.review-actions span,.script-review p{color:var(--muted);font-size:11px;line-height:1.8}.script-review details{margin:12px 0;padding:12px;background:#f5f7f2;border-radius:8px}.script-review article{border-bottom:1px solid var(--line);padding:10px 0}.script-review article strong{font-size:12px}.rewrite-preview{padding:14px;border:1px solid var(--line);border-radius:8px;margin-top:12px}.rewrite-preview h4{font-size:13px}.rewrite-preview del{color:#956247}
</style>
