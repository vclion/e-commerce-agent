<script setup lang="ts">
import {computed} from 'vue'
const props = defineProps<{category: string; color?: string | number}>()
const fill = computed(() => {
  const c = String(props.color || '')
  if (/黑|炭/.test(c)) return '#343b3e'
  if (/蓝|青/.test(c)) return '#677e93'
  if (/绿/.test(c)) return '#7e8c77'
  if (/粉|桃|紫/.test(c)) return '#b99197'
  if (/白|奶|杏|黄/.test(c)) return '#e5dace'
  if (/灰|银/.test(c)) return '#969b9d'
  if (/棕|咖|驼|糖|褐/.test(c)) return '#9d7963'
  return '#b4a48d'
})
const bottom = computed(() => ['长裤', '短裤'].includes(props.category))
</script>
<template>
  <svg viewBox="0 0 240 260" class="garment-art" role="img" :aria-label="`${color || ''}${category}款式示意，非商品实拍`">
    <ellipse cx="120" cy="242" rx="69" ry="7" fill="#293a32" opacity=".07"/>
    <g :fill="fill" stroke="#394139" stroke-opacity=".27" stroke-width="1.5" stroke-linejoin="round">
      <template v-if="bottom"><path :d="category === '短裤' ? 'M68 47 H172 L186 173 L127 179 L120 104 L113 179 L54 173 Z' : 'M68 36 H172 L180 228 L130 228 L120 112 L110 228 L60 228 Z'"/><path d="M68 49 H172 M120 49 V100 M78 55 Q80 77 67 83 M162 55 Q160 77 173 83" fill="none"/><path d="M88 48 V60 M152 48 V60" fill="none"/></template>
      <template v-else-if="category === '半身裙'"><path d="M87 46 H153 L187 227 Q120 241 53 227 Z"/><path d="M87 57 H153 M105 65 L94 224 M135 65 L146 224" fill="none"/></template>
      <template v-else-if="category === '连衣裙'"><path d="M94 29 Q120 44 146 29 L174 43 L185 91 L158 99 L148 78 L141 112 L183 227 Q120 243 57 227 L99 112 L92 78 L82 99 L55 91 L66 43 Z"/><path d="M100 111 Q120 117 140 111 M107 123 L90 224 M132 123 L151 224" fill="none"/><path d="M106 31 Q120 62 134 31" fill="none"/></template>
      <template v-else><path :d="['风衣','大衣'].includes(category) ? 'M94 29 L65 42 L31 154 L57 164 L80 91 L72 229 H168 L160 91 L183 164 L209 154 L175 42 L146 29 Q120 42 94 29 Z' : 'M94 39 L65 52 L28 147 L56 159 L79 104 L75 210 Q120 221 165 210 L161 104 L184 159 L212 147 L175 52 L146 39 Q120 55 94 39 Z'"/>
        <path v-if="['衬衫','西装','夹克','风衣','大衣'].includes(category)" d="M94 39 L108 68 L120 52 L132 68 L146 39 M120 54 V211" fill="none"/>
        <path v-else d="M95 40 Q120 79 145 40 M80 199 Q120 208 160 199" fill="none"/>
        <path v-if="category === '羽绒服'" d="M79 96 H161 M78 124 H162 M77 152 H163 M77 180 H163" fill="none"/>
        <path v-if="['衬衫','夹克','大衣'].includes(category)" d="M135 89 H155 V110 H135 Z" fill="none"/>
        <g v-if="category === '衬衫'" fill="#f4efe7" stroke="none"><circle cx="123" cy="88" r="1.6"/><circle cx="123" cy="113" r="1.6"/><circle cx="123" cy="138" r="1.6"/><circle cx="123" cy="163" r="1.6"/></g>
      </template>
    </g>
  </svg>
</template>
