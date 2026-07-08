<template>
  <div class="task-board-overlay" v-if="visible" @click.self="$emit('close')">
    <div class="task-board-panel" :class="{ collapsed: panelCollapsed }">
      <!-- Header -->
      <div class="tb-header">
        <div class="tb-header-left">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
            <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
            <rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>
          </svg>
          <span class="tb-title">任务看板</span>
          <span class="tb-stats-badge" v-if="stats.total > 0">
            {{ stats.completed }}/{{ stats.total }}
          </span>
        </div>
        <div class="tb-header-right">
          <button class="tb-btn tb-btn-ghost" @click="refreshBoard" :disabled="loading" title="刷新">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"
              :class="{ 'spin-icon': loading }">
              <path d="M1 4v6h6M23 20v-6h-6"/>
              <path d="M20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
            </svg>
          </button>
          <button class="tb-btn tb-btn-ghost" @click="showCreateTask = true" title="新建任务">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="14" height="14">
              <path d="M12 5v14M5 12h14"/>
            </svg>
          </button>
          <button class="tb-btn tb-btn-ghost" @click="$emit('close')" title="关闭">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
              <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- Overall Progress Bar -->
      <div class="tb-overall-progress" v-if="stats.total > 0">
        <div class="tb-progress-info">
          <span>总进度</span>
          <span class="tb-progress-pct">{{ stats.overall_progress }}%</span>
        </div>
        <div class="tb-progress-track">
          <div class="tb-progress-fill" :style="{ width: stats.overall_progress + '%' }"
            :class="{ complete: stats.overall_progress >= 100, stuck: stats.stuck > 0 }"></div>
        </div>
        <div class="tb-progress-detail">
          <span class="tb-stat-dot in-progress"></span> 进行中 {{ stats.in_progress }}
          <span class="tb-stat-dot pending"></span> 待处理 {{ stats.pending }}
          <span class="tb-stat-dot completed"></span> 完成 {{ stats.completed }}
          <span class="tb-stat-dot failed" v-if="stats.failed > 0"></span>
          <span v-if="stats.failed > 0"> 失败 {{ stats.failed }}</span>
          <span class="tb-stat-dot stuck" v-if="stats.stuck > 0"></span>
          <span v-if="stats.stuck > 0" class="stuck-text"> 卡住 {{ stats.stuck }}</span>
        </div>
      </div>

      <!-- Agent Progress Cards -->
      <div class="tb-agent-section" v-if="Object.keys(agentSummary).length > 0">
        <div class="tb-section-title">智能体进度</div>
        <div class="tb-agent-cards">
          <div v-for="(info, agentId) in agentSummary" :key="agentId" class="tb-agent-card"
            :class="{ streaming: info.is_streaming, stuck: info.stuck_count > 0 }">
            <div class="tb-agent-card-header">
              <div class="tb-agent-avatar" :style="{ background: getAgentColor(store.agents.findIndex(a => a.id === agentId)) }">
                <span v-html="getAgentIcon(store.agents.findIndex(a => a.id === agentId))"></span>
              </div>
              <div class="tb-agent-info">
                <span class="tb-agent-name">{{ info.name }}</span>
                <span class="tb-agent-status">
                  <span v-if="info.is_streaming" class="status-badge streaming">工作中</span>
                  <span v-else-if="info.stuck_count > 0" class="status-badge stuck">卡住</span>
                  <span v-else-if="info.in_progress > 0" class="status-badge active">进行中</span>
                  <span v-else class="status-badge idle">空闲</span>
                </span>
              </div>
              <span class="tb-agent-pct">{{ info.progress }}%</span>
            </div>
            <div class="tb-agent-progress">
              <div class="tb-progress-track small">
                <div class="tb-progress-fill" :style="{ width: info.progress + '%' }"
                  :class="{ complete: info.progress >= 100, stuck: info.stuck_count > 0 }"></div>
              </div>
            </div>
            <div class="tb-agent-detail">
              <span>{{ info.completed }}/{{ info.total_tasks }} 任务完成</span>
              <span v-if="info.failed > 0" class="failed-text">{{ info.failed }} 失败</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Kanban Columns -->
      <div class="tb-columns" v-if="hasAnyTasks">
        <!-- In Progress Column -->
        <div class="tb-column" v-if="board.in_progress.length > 0">
          <div class="tb-col-header in-progress">
            <span class="tb-col-dot"></span>
            <span>进行中</span>
            <span class="tb-col-count">{{ board.in_progress.length }}</span>
          </div>
          <div class="tb-col-body">
            <div v-for="task in board.in_progress" :key="task.id" class="tb-task-card"
              :class="{ streaming: task.streaming, stuck: task.stuck_analysis?.is_stuck }">
              <div class="tb-task-title">{{ task.title }}</div>
              <div class="tb-task-meta">
                <span v-if="task.assigned_agent" class="tb-task-agent">
                  {{ getAgentName(task.assigned_agent) }}
                </span>
                <span v-if="task.streaming" class="tb-task-status streaming">
                  <span class="mini-spinner"></span> 生成中
                </span>
                <span v-else-if="task.stuck_analysis?.is_stuck" class="tb-task-status stuck">
                  ⚠ 卡住
                </span>
              </div>
              <!-- Tool call progress -->
              <div v-if="task.streaming && task.stream_tool_calls > 0" class="tb-task-tools">
                <span class="tool-count">🔧 {{ task.stream_tool_calls }} 个工具调用</span>
              </div>
              <!-- Stuck analysis -->
              <div v-if="task.stuck_analysis?.is_stuck" class="tb-stuck-box">
                <div class="stuck-reason" v-for="(reason, i) in task.stuck_analysis.reasons" :key="i">
                  {{ reason }}
                </div>
                <div class="stuck-suggestion" v-for="(sug, i) in task.stuck_analysis.suggestions" :key="'s'+i">
                  💡 {{ sug }}
                </div>
              </div>
              <div class="tb-task-actions">
                <button class="tb-task-btn" @click="editTask(task)" title="编辑">✏️</button>
                <button class="tb-task-btn" @click="deleteTask(task)" title="删除">🗑️</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Pending Column -->
        <div class="tb-column" v-if="board.pending.length > 0">
          <div class="tb-col-header pending">
            <span class="tb-col-dot"></span>
            <span>待处理</span>
            <span class="tb-col-count">{{ board.pending.length }}</span>
          </div>
          <div class="tb-col-body">
            <div v-for="task in board.pending" :key="task.id" class="tb-task-card"
              :class="{ stuck: task.stuck_analysis?.is_stuck }">
              <div class="tb-task-title">{{ task.title }}</div>
              <div class="tb-task-meta">
                <span v-if="task.assigned_agent" class="tb-task-agent">
                  {{ getAgentName(task.assigned_agent) }}
                </span>
                <span v-if="task.stuck_analysis?.is_stuck" class="tb-task-status stuck">
                  ⚠ 等待中
                </span>
              </div>
              <div v-if="task.stuck_analysis?.is_stuck" class="tb-stuck-box">
                <div class="stuck-reason" v-for="(reason, i) in task.stuck_analysis.reasons" :key="i">
                  {{ reason }}
                </div>
                <div class="stuck-suggestion" v-for="(sug, i) in task.stuck_analysis.suggestions" :key="'s'+i">
                  💡 {{ sug }}
                </div>
              </div>
              <div class="tb-task-actions">
                <button class="tb-task-btn primary" @click="startTask(task)" title="开始">▶</button>
                <button class="tb-task-btn" @click="editTask(task)" title="编辑">✏️</button>
                <button class="tb-task-btn" @click="deleteTask(task)" title="删除">🗑️</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Completed Column -->
        <div class="tb-column" v-if="board.completed.length > 0">
          <div class="tb-col-header completed">
            <span class="tb-col-dot"></span>
            <span>已完成</span>
            <span class="tb-col-count">{{ board.completed.length }}</span>
          </div>
          <div class="tb-col-body">
            <div v-for="task in board.completed" :key="task.id" class="tb-task-card done">
              <div class="tb-task-title">{{ task.title }}</div>
              <div class="tb-task-meta">
                <span v-if="task.assigned_agent" class="tb-task-agent">
                  {{ getAgentName(task.assigned_agent) }}
                </span>
                <span class="tb-task-status done">✓ 完成</span>
              </div>
              <div class="tb-task-actions">
                <button class="tb-task-btn" @click="deleteTask(task)" title="删除">🗑️</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Failed Column -->
        <div class="tb-column" v-if="board.failed.length > 0">
          <div class="tb-col-header failed">
            <span class="tb-col-dot"></span>
            <span>失败</span>
            <span class="tb-col-count">{{ board.failed.length }}</span>
          </div>
          <div class="tb-col-body">
            <div v-for="task in board.failed" :key="task.id" class="tb-task-card failed">
              <div class="tb-task-title">{{ task.title }}</div>
              <div class="tb-task-meta">
                <span v-if="task.assigned_agent" class="tb-task-agent">
                  {{ getAgentName(task.assigned_agent) }}
                </span>
                <span class="tb-task-status failed">✗ 失败</span>
              </div>
              <div v-if="task.stuck_analysis" class="tb-stuck-box">
                <div class="stuck-reason" v-for="(reason, i) in task.stuck_analysis.reasons" :key="i">
                  {{ reason }}
                </div>
                <div class="stuck-suggestion" v-for="(sug, i) in task.stuck_analysis.suggestions" :key="'s'+i">
                  💡 {{ sug }}
                </div>
              </div>
              <div class="tb-task-actions">
                <button class="tb-task-btn primary" @click="retryTask(task)" title="重试">🔄</button>
                <button class="tb-task-btn" @click="deleteTask(task)" title="删除">🗑️</button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <div v-if="!hasAnyTasks && !loading" class="tb-empty">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="48" height="48">
          <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
          <rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>
        </svg>
        <div class="tb-empty-text">暂无任务</div>
        <div class="tb-empty-hint">点击右上角 + 创建任务，或在聊天中 @智能体 开始工作</div>
        <button class="tb-btn tb-btn-primary" @click="showCreateTask = true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="14" height="14">
            <path d="M12 5v14M5 12h14"/>
          </svg>
          新建任务
        </button>
      </div>

      <!-- Create Task Dialog -->
      <div v-if="showCreateTask" class="tb-dialog-overlay" @click.self="showCreateTask = false">
        <div class="tb-dialog">
          <div class="tb-dialog-header">
            <span>{{ editingTask ? '编辑任务' : '新建任务' }}</span>
            <button class="tb-btn tb-btn-ghost" @click="showCreateTask = false">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
                <path d="M18 6L6 18M6 6l12 12"/>
              </svg>
            </button>
          </div>
          <div class="tb-dialog-body">
            <label class="tb-label">任务标题</label>
            <input v-model="taskForm.title" class="tb-input" placeholder="例如：实现用户登录功能" ref="taskTitleInput" />
            <label class="tb-label">描述（可选）</label>
            <textarea v-model="taskForm.description" class="tb-textarea" rows="2" placeholder="任务详情..."></textarea>
            <label class="tb-label">分配给</label>
            <select v-model="taskForm.assigned_agent" class="tb-select">
              <option value="">未分配</option>
              <option v-for="m in aiMembers" :key="m.id" :value="m.id">{{ m.name }}</option>
            </select>
            <label class="tb-label">优先级</label>
            <div class="tb-priority-row">
              <button v-for="p in ['low','normal','high','urgent']" :key="p"
                class="tb-priority-btn" :class="{ active: taskForm.priority === p }"
                @click="taskForm.priority = p">
                {{ { low: '低', normal: '普通', high: '高', urgent: '紧急' }[p] }}
              </button>
            </div>
          </div>
          <div class="tb-dialog-footer">
            <button class="tb-btn tb-btn-ghost" @click="showCreateTask = false">取消</button>
            <button class="tb-btn tb-btn-primary" @click="saveTask" :disabled="!taskForm.title.trim()">
              {{ editingTask ? '保存' : '创建' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { store, api, getAgentColor, getAgentIcon } from '../store.js'

const props = defineProps({
  roomId: { type: String, required: true },
  visible: { type: Boolean, default: false },
  members: { type: Array, default: () => [] },
})
const emit = defineEmits(['close'])

const loading = ref(false)
const board = ref({ pending: [], in_progress: [], completed: [], failed: [] })
const agentSummary = ref({})
const stats = ref({ total: 0, completed: 0, in_progress: 0, pending: 0, failed: 0, stuck: 0, overall_progress: 0 })

const showCreateTask = ref(false)
const editingTask = ref(null)
const taskForm = ref({ title: '', description: '', assigned_agent: '', priority: 'normal' })

const panelCollapsed = ref(false)
const taskTitleInput = ref(null)

const aiMembers = computed(() => props.members.filter(m => m.id !== 'user' && m.id !== 'system'))

const hasAnyTasks = computed(() =>
  board.value.pending.length > 0 || board.value.in_progress.length > 0 ||
  board.value.completed.length > 0 || board.value.failed.length > 0
)

function getAgentName(id) {
  const agent = store.agents.find(a => a.id === id)
  const member = props.members.find(m => m.id === id)
  return agent?.name || member?.name || id.slice(0, 8)
}

async function refreshBoard() {
  loading.value = true
  try {
    const data = await api('GET', `/admin/rooms/${props.roomId}/task-board`)
    if (data.ok) {
      board.value = data.result.board
      agentSummary.value = data.result.agent_summary
      stats.value = data.result.stats
    }
  } catch (e) {
    console.error('[TaskBoard] refresh error:', e)
  } finally {
    loading.value = false
  }
}

async function saveTask() {
  const title = taskForm.value.title.trim()
  if (!title) return

  if (editingTask.value) {
    await api('PATCH', `/admin/tasks/${editingTask.value.id}`, {
      title,
      description: taskForm.value.description,
      assigned_agent: taskForm.value.assigned_agent || null,
      priority: taskForm.value.priority,
    })
  } else {
    await api('POST', `/admin/rooms/${props.roomId}/tasks`, {
      title,
      description: taskForm.value.description,
      assigned_agent: taskForm.value.assigned_agent || null,
      priority: taskForm.value.priority,
    })
  }

  showCreateTask.value = false
  editingTask.value = null
  taskForm.value = { title: '', description: '', assigned_agent: '', priority: 'normal' }
  refreshBoard()
}

function editTask(task) {
  editingTask.value = task
  taskForm.value = {
    title: task.title,
    description: task.description || '',
    assigned_agent: task.assigned_agent || '',
    priority: task.priority || 'normal',
  }
  showCreateTask.value = true
  nextTick(() => taskTitleInput.value?.focus())
}

async function deleteTask(task) {
  if (!confirm(`确定删除任务「${task.title}」？`)) return
  await api('DELETE', `/admin/tasks/${task.id}`)
  refreshBoard()
}

async function startTask(task) {
  await api('PATCH', `/admin/tasks/${task.id}`, { status: 'in_progress' })
  refreshBoard()
}

async function retryTask(task) {
  await api('PATCH', `/admin/tasks/${task.id}`, { status: 'pending' })
  refreshBoard()
}

// Auto-refresh
let refreshTimer = null

watch(() => props.visible, (v) => {
  if (v) {
    refreshBoard()
    refreshTimer = setInterval(refreshBoard, 5000)
  } else {
    clearInterval(refreshTimer)
  }
})

onMounted(() => {
  if (props.visible) {
    refreshBoard()
    refreshTimer = setInterval(refreshBoard, 5000)
  }
})

onUnmounted(() => {
  clearInterval(refreshTimer)
})
</script>

<style scoped>
.task-board-overlay {
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.3);
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
  animation: fadeIn 0.2s ease;
}

@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

.task-board-panel {
  width: 420px;
  max-width: 90vw;
  height: 100vh;
  background: var(--bg-primary, #1a1a2e);
  border-left: 1px solid var(--border, #2a2a4a);
  display: flex;
  flex-direction: column;
  animation: slideIn 0.25s ease;
  overflow: hidden;
}

@keyframes slideIn { from { transform: translateX(100%); } to { transform: translateX(0); } }

/* Header */
.tb-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border, #2a2a4a);
  background: var(--bg-secondary, #16162a);
}
.tb-header-left { display: flex; align-items: center; gap: 8px; }
.tb-header-right { display: flex; align-items: center; gap: 4px; }
.tb-title { font-size: 15px; font-weight: 600; color: var(--text-primary, #e0e0e0); }
.tb-stats-badge {
  font-size: 11px; padding: 2px 8px; border-radius: 10px;
  background: rgba(64,145,108,0.2); color: #40916c; font-weight: 600;
}

/* Overall Progress */
.tb-overall-progress { padding: 14px 16px; border-bottom: 1px solid var(--border, #2a2a4a); }
.tb-progress-info { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-dim, #888); margin-bottom: 6px; }
.tb-progress-pct { font-weight: 700; color: var(--text-primary, #e0e0e0); font-size: 14px; }
.tb-progress-track { height: 6px; background: var(--bg-tertiary, #22223a); border-radius: 3px; overflow: hidden; }
.tb-progress-track.small { height: 4px; }
.tb-progress-fill { height: 100%; background: linear-gradient(90deg, #40916c, #52b788); border-radius: 3px; transition: width 0.5s ease; }
.tb-progress-fill.complete { background: linear-gradient(90deg, #40916c, #2d6a4f); }
.tb-progress-fill.stuck { background: linear-gradient(90deg, #d97706, #dc2626); }
.tb-progress-detail { display: flex; gap: 12px; margin-top: 8px; font-size: 11px; color: var(--text-dim, #888); flex-wrap: wrap; }
.tb-stat-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 3px; vertical-align: middle; }
.tb-stat-dot.in-progress { background: #52b788; }
.tb-stat-dot.pending { background: #888; }
.tb-stat-dot.completed { background: #40916c; }
.tb-stat-dot.failed { background: #dc2626; }
.tb-stat-dot.stuck { background: #d97706; }
.stuck-text { color: #d97706; font-weight: 600; }

/* Agent Cards */
.tb-agent-section { padding: 12px 16px; border-bottom: 1px solid var(--border, #2a2a4a); }
.tb-section-title { font-size: 12px; font-weight: 600; color: var(--text-dim, #888); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 10px; }
.tb-agent-cards { display: flex; flex-direction: column; gap: 8px; }
.tb-agent-card {
  padding: 10px 12px; border-radius: 8px;
  background: var(--bg-secondary, #16162a); border: 1px solid var(--border, #2a2a4a);
  transition: all 0.3s ease;
}
.tb-agent-card.streaming { border-color: rgba(82,183,136,0.4); box-shadow: 0 0 12px rgba(82,183,136,0.1); }
.tb-agent-card.stuck { border-color: rgba(217,119,6,0.4); box-shadow: 0 0 12px rgba(217,119,6,0.1); }
.tb-agent-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.tb-agent-avatar { width: 28px; height: 28px; border-radius: 6px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.tb-agent-avatar :deep(svg) { width: 16px; height: 16px; color: white; }
.tb-agent-info { flex: 1; min-width: 0; }
.tb-agent-name { font-size: 13px; font-weight: 600; color: var(--text-primary, #e0e0e0); display: block; }
.tb-agent-status { font-size: 11px; }
.status-badge { padding: 1px 6px; border-radius: 4px; font-size: 10px; font-weight: 600; }
.status-badge.streaming { background: rgba(82,183,136,0.2); color: #52b788; }
.status-badge.stuck { background: rgba(217,119,6,0.2); color: #d97706; }
.status-badge.active { background: rgba(64,145,108,0.15); color: #40916c; }
.status-badge.idle { background: rgba(136,136,136,0.15); color: #888; }
.tb-agent-pct { font-size: 14px; font-weight: 700; color: var(--text-primary, #e0e0e0); }
.tb-agent-progress { margin-top: 4px; }
.tb-agent-detail { font-size: 11px; color: var(--text-dim, #888); margin-top: 4px; display: flex; gap: 8px; }
.failed-text { color: #dc2626; }

/* Kanban Columns */
.tb-columns { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 16px; }
.tb-column {}
.tb-col-header { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.tb-col-dot { width: 8px; height: 8px; border-radius: 50%; }
.tb-col-header.in-progress .tb-col-dot { background: #52b788; }
.tb-col-header.pending .tb-col-dot { background: #888; }
.tb-col-header.completed .tb-col-dot { background: #40916c; }
.tb-col-header.failed .tb-col-dot { background: #dc2626; }
.tb-col-count { font-size: 11px; color: var(--text-dim, #888); margin-left: auto; }
.tb-col-body { display: flex; flex-direction: column; gap: 6px; }

/* Task Cards */
.tb-task-card {
  padding: 10px 12px; border-radius: 8px;
  background: var(--bg-secondary, #16162a); border: 1px solid var(--border, #2a2a4a);
  transition: all 0.2s ease;
}
.tb-task-card:hover { border-color: rgba(255,255,255,0.1); }
.tb-task-card.streaming { border-color: rgba(82,183,136,0.3); }
.tb-task-card.stuck { border-color: rgba(217,119,6,0.3); }
.tb-task-card.done { opacity: 0.7; }
.tb-task-card.failed { border-color: rgba(220,38,38,0.3); }
.tb-task-title { font-size: 13px; font-weight: 500; color: var(--text-primary, #e0e0e0); margin-bottom: 4px; }
.tb-task-meta { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--text-dim, #888); }
.tb-task-agent { background: rgba(255,255,255,0.06); padding: 1px 6px; border-radius: 4px; }
.tb-task-status { font-weight: 600; }
.tb-task-status.streaming { color: #52b788; }
.tb-task-status.stuck { color: #d97706; }
.tb-task-status.done { color: #40916c; }
.tb-task-status.failed { color: #dc2626; }
.tb-task-tools { margin-top: 4px; font-size: 11px; color: var(--text-dim, #888); }
.tool-count { background: rgba(255,255,255,0.05); padding: 1px 6px; border-radius: 4px; }

/* Stuck Box */
.tb-stuck-box {
  margin-top: 8px; padding: 8px 10px; border-radius: 6px;
  background: rgba(217,119,6,0.08); border: 1px solid rgba(217,119,6,0.15);
}
.stuck-reason { font-size: 11px; color: #d97706; margin-bottom: 4px; font-weight: 500; }
.stuck-suggestion { font-size: 11px; color: var(--text-dim, #999); margin-top: 2px; }

/* Task Actions */
.tb-task-actions { display: flex; gap: 4px; margin-top: 6px; }
.tb-task-btn {
  background: none; border: none; cursor: pointer; font-size: 12px;
  padding: 2px 6px; border-radius: 4px; transition: background 0.15s;
}
.tb-task-btn:hover { background: rgba(255,255,255,0.08); }
.tb-task-btn.primary { color: #52b788; }

/* Empty State */
.tb-empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 40px; color: var(--text-dim, #888); }
.tb-empty-text { font-size: 15px; font-weight: 500; }
.tb-empty-hint { font-size: 12px; text-align: center; max-width: 260px; }

/* Dialog */
.tb-dialog-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.4); z-index: 1001; display: flex; align-items: center; justify-content: center; }
.tb-dialog { width: 380px; max-width: 90vw; background: var(--bg-primary, #1a1a2e); border: 1px solid var(--border, #2a2a4a); border-radius: 12px; overflow: hidden; }
.tb-dialog-header { display: flex; justify-content: space-between; align-items: center; padding: 14px 16px; border-bottom: 1px solid var(--border, #2a2a4a); font-size: 15px; font-weight: 600; color: var(--text-primary, #e0e0e0); }
.tb-dialog-body { padding: 16px; }
.tb-dialog-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border, #2a2a4a); }

.tb-label { display: block; font-size: 12px; font-weight: 500; color: var(--text-dim, #888); margin-bottom: 4px; margin-top: 10px; }
.tb-label:first-child { margin-top: 0; }
.tb-input, .tb-textarea, .tb-select {
  width: 100%; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border, #2a2a4a);
  background: var(--bg-secondary, #16162a); color: var(--text-primary, #e0e0e0); font-size: 13px;
  outline: none; box-sizing: border-box;
}
.tb-input:focus, .tb-textarea:focus, .tb-select:focus { border-color: #40916c; }
.tb-textarea { resize: vertical; }
.tb-select { cursor: pointer; }

.tb-priority-row { display: flex; gap: 6px; }
.tb-priority-btn {
  flex: 1; padding: 6px; border-radius: 6px; border: 1px solid var(--border, #2a2a4a);
  background: var(--bg-secondary, #16162a); color: var(--text-dim, #888);
  font-size: 12px; cursor: pointer; transition: all 0.15s;
}
.tb-priority-btn.active { border-color: #40916c; color: #40916c; background: rgba(64,145,108,0.1); }

/* Buttons */
.tb-btn {
  display: inline-flex; align-items: center; gap: 4px; padding: 6px 12px;
  border-radius: 6px; border: none; font-size: 13px; cursor: pointer; transition: all 0.15s;
}
.tb-btn-ghost { background: none; color: var(--text-dim, #888); }
.tb-btn-ghost:hover { background: rgba(255,255,255,0.08); color: var(--text-primary, #e0e0e0); }
.tb-btn-primary { background: #40916c; color: white; font-weight: 500; }
.tb-btn-primary:hover { background: #52b788; }
.tb-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

/* Spinner */
.mini-spinner {
  display: inline-block; width: 8px; height: 8px; border: 1.5px solid rgba(82,183,136,0.3);
  border-top-color: #52b788; border-radius: 50%; animation: spin 0.8s linear infinite;
  vertical-align: middle; margin-right: 2px;
}
.spin-icon { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Scrollbar */
.tb-columns::-webkit-scrollbar, .tb-col-body::-webkit-scrollbar { width: 4px; }
.tb-columns::-webkit-scrollbar-track, .tb-col-body::-webkit-scrollbar-track { background: transparent; }
.tb-columns::-webkit-scrollbar-thumb, .tb-col-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
</style>
