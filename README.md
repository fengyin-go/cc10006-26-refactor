# 数据湖仓一体管理平台

面向数据湖仓一体架构下的多源异构接入、原始文件编目、湖内分区治理、流批一体转换、版本回溯与数据编目检索的管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据编目 | `catalog` | 数据目录 | 编目编号、数据集名称、存储路径 |
| 多源接入 | `ingest` | 接入通道 | 通道编号、源端类型、连接参数 |
| 原始层管理 | `bronze` | 原始表 | 表编号、来源通道、目标路径 |
| 清洗层管理 | `silver` | 清洗表 | 表编号、上游原始表、清洗脚本 |
| 聚合层管理 | `gold` | 聚合视图 | 视图编号、上游清洗表、聚合维度 |
| 湖区分区 | `lakezone` | 分区方案 | 方案编号、目标表名、分区字段 |
| 流式处理 | `stream` | 流处理作业 | 作业编号、输入Topic、处理逻辑 |
| 批量转换 | `batch` | 批处理任务 | 任务编号、输入路径、处理脚本 |
| 版本快照 | `snapshot` | 快照记录 | 快照编号、目标表名、快照时间 |
| 湖仓治理 | `govern` | 治理规则 | 规则编号、治理对象、治理类型 |
| 元数据中心 | `meta` | 元数据条目 | 条目编号、对象类型、对象名称 |
| 血缘图谱 | `lineage` | 血缘节点 | 节点编号、节点类型、节点名称 |
| 质量监控 | `quality` | 质量指标 | 指标编号、监控对象、指标类型 |
| 编排调度 | `schedule` | 编排计划 | 计划编号、任务列表、依赖图定义 |
| 历史回填 | `backfill` | 回填作业 | 作业编号、目标表名、回填起始 |
| 数据检索 | `search` | 检索索引 | 索引编号、索引名称、覆盖表名 |
| 资源计量 | `resource` | 计量记录 | 记录编号、计算引擎、作业名称 |
| 操作审计 | `audit` | 审计日志 | 日志编号、操作类型、操作对象 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 批处理执行契约

批量转换、编排调度和批处理回执共用一份执行契约：

- 模型定义在 `backend/app/services/batch_models.py`：状态映射、动作契约、
  `BatchTaskDefinition`（处理脚本/依赖上游等任务字段）和 `ExecutionReceipt`。
- 状态推进只在 `backend/app/services/batch_execution.py`：每个任务只有一条执行
  记录，页面动作、调度器超时、批处理回执写入都落在它上面。
- 规范状态为 `draft/scheduled/running/succeeded/failed`；对外 `status` 仍输出中文
  （草稿/待调度/运行中/已完成/已失败），旧页面与导出的中文字段保持不变。
- 已完成/已失败是终态，任何新动作都不能跳级覆盖；重复请求按幂等键或同向动作
  返回同一条回执，不会重复写审计。
- 旧任务记录没有执行记录时按原字段补齐，原动作（提交调度/启动任务/终止任务/
  归档结果）仍落到原来的目标状态；状态冲突以最新一条有效回执为准。
- 关联的编排计划会带出相同执行记录的 `execution_status` / `final_status`；审计
  日志记录 `操作来源`（页面/调度器/批处理）与 `最终状态`。

相关接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/batch/actions` | 动作契约与状态顺序 |
| `GET /api/batch/executions` | 全部执行记录 |
| `GET /api/batch/{id}/execution` | 单任务执行记录与回执链 |
| `POST /api/batch/{id}/actions` | 页面动作，兼容 `{values:{action}}` |
| `POST /api/batch/{id}/timeout` | 调度器超时结束 |
| `POST /api/batch/receipts` | 批处理回执写入完成/失败 |
| `GET /api/schedule/{id}/executions` | 计划关联的执行记录 |
| `POST /api/schedule/{id}/timeout` | 调度器结束计划内任务 |

领域：数据工程
子方向：数仓与数据湖
