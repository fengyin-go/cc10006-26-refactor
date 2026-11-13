<template>
  <section class="page" data-module="batch">
    <header class="page-head">
      <div>
        <h2>批量转换管理</h2>
        <p class="page-desc">维护批量转换任务的输入、执行约束与结果记录，为批量转换提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记批处理任务</button>
        <button class="btn" type="button" @click="exportRows">导出批量转换清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无批量转换数据，可先登记批处理任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条批量转换记录</span>
      <span v-if="lastReceipt">最近回执：{{ lastReceipt.动作 }} · {{ lastReceipt.最终状态 }} · 来源 {{ lastReceipt.触发来源 }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/batch'
const columns = ["任务编号", "输入路径", "处理脚本", "输出路径", "分区覆盖", "依赖上游", "重试策略", "超时终止", "执行状态", "终态", "触发来源"]
const actions = ["提交调度", "启动任务", "终止任务", "归档结果"]
const statuses = ["草稿", "待调度", "运行中", "已完成", "已失败"]
const stats = [{"label": "批处理任务总数", "value": 0}, {"label": "运行中任务", "value": 0}, {"label": "已完成任务", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const lastReceipt = ref<Record<string, string> | null>(null)
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '批处理任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, source: 'page' }),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.message || '批量转换动作未生效，请稍后重试')
    }
    lastReceipt.value = payload.receipt ?? null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量转换操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('批处理任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量转换列表读取失败'
  }
}

onMounted(reload)
</script>
