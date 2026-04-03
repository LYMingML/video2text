# video2text Polling Mechanism Analysis

## Overview
This document analyzes the current polling mechanism in video2text and provides detailed information for converting it to Server-Sent Events (SSE).

---

## 1. Backend Architecture (fastapi_app.py)

### 1.1 JobState Class Definition
**Location:** Lines 119-146

```python
@dataclass
class JobState:
    job_id: str
    status: str = "等待中"
    plain_text: str = ""
    logs: list[str] = field(default_factory=list)
    current_job: str = ""
    current_prefix: str = ""
    zip_bundle: str | None = None
    done: bool = False
    failed: bool = False
    running: bool = False
    progress_pct: int = 0
    eta_seconds: int = 0
    step_label: str = ""
    updated_at: float = field(default_factory=time.time)
    # Queue-related
    video_path: str = ""
    translate_params: dict = field(default_factory=dict)
    display_name: str = ""
    auto_translate: bool = False
    auto_download: bool = False

    def add_log(self, msg: str):
        self.logs.append(msg)
        if len(self.logs) > 500:
            self.logs = self.logs[-350:]
```

**Key Fields:**
- `job_id`: Unique identifier for the job
- `status`: Current status message (displayed in UI)
- `plain_text`: Accumulated transcription/translation text
- `logs`: List of log messages
- `done`, `failed`, `running`: State flags
- `progress_pct`: Progress percentage (0-100)
- `eta_seconds`: Estimated time remaining
- `step_label`: Current step label (e.g., "识别完成", "翻译中")
- `updated_at`: Timestamp of last update
- `auto_translate`, `auto_download`: Auto-action flags

---

### 1.2 Global State Management
**Location:** Lines 148-154

```python
_RUNTIME_LOCK = threading.RLock()  # Reentrant lock to avoid deadlocks
_RUNTIME_JOB: JobState | None = None    # Currently running job
_RUNTIME_THREAD: threading.Thread | None = None  # Worker thread

# Task queue system
_TRANSCRIBE_QUEUE: list[str] = []       # Queue of job IDs
_ALL_JOBS: dict[str, JobState] = {}      # All jobs dictionary
```

**_RUNTIME_LOCK Usage Pattern:**
- Used for thread-safe access to `_RUNTIME_JOB`, `_ALL_JOBS`, `_TRANSCRIBE_QUEUE`
- Applied before any read/write operations on job state
- Ensures consistency when workers update job fields

**Key Usage Points:**
- Line 294: `_get_job()` - Safely retrieve job from `_ALL_JOBS`
- Line 303: `_set_job_state()` - Set current running job
- Line 349: `_get_queue_status()` - Clean old jobs and return status
- Line 373: `_schedule_next_transcribe()` - Queue management
- Lines 580, 628, 632: Worker functions updating job state
- Lines 821, 825, 908, 912: Translation worker state updates
- Lines 3761, 4363, 4474, 4564, 4679: API endpoints with state changes

---

### 1.3 Job State Update Functions

#### _set_job_progress() Function
**Location:** Lines 456-487

**Purpose:** Updates job progress fields during worker execution

**Signature:**
```python
def _set_job_progress(
    job: JobState,
    status: str,
    start_ts: float,
    *,
    progress_pct: int | None = None,
    eta_seconds: float | None = None,
    step_label: str | None = None,
):
```

**What it does:**
1. Estimates percentage from status string if not provided
2. Calculates ETA based on elapsed time if not provided
3. Extracts step label from status
4. Decorates status with progress info (e.g., "识别中｜总进度 45%｜预计剩余 00:05:30")
5. Updates job fields:
   - `job.status` (decorated string)
   - `job.progress_pct` (0-100)
   - `job.eta_seconds` (estimated remaining time)
   - `job.step_label` (current step)
   - `job.updated_at` (current timestamp)

**Callers:**
- Line 388: `_schedule_next_transcribe()` - Initial job start
- Line 475: Core transcribe stream (via worker)
- Line 579, 619, 636: `_run_transcribe_worker()` - Various states
- Line 771, 819, 829: `_run_subtitle_import_worker()` - Subtitle import
- Line 861, 868, 890, 900: `_run_translate_worker()` - Translation stages
- Line 4387, 4493, 4570: API endpoints

---

#### _json_job() Function
**Location:** Lines 307-344

**Purpose:** Serializes job state to JSON for API responses

**Signature:**
```python
def _json_job(job: JobState) -> dict[str, Any]:
```

**Returns:**
```python
{
    "job_id": job.job_id,
    "status": job.status,
    "plain_text": job.plain_text,
    "log_text": "\n".join(job.logs),
    "current_job": job.current_job,
    "current_prefix": job.current_prefix,
    "zip_ready": bool(job.zip_bundle and Path(job.zip_bundle).exists()),
    "done": job.done,
    "failed": job.failed,
    "running": job.running,
    "progress_pct": max(0, min(100, int(job.progress_pct))),
    "eta_seconds": max(0, int(job.eta_seconds)),
    "step_label": job.step_label,
    "updated_at": job.updated_at,
    "display_name": job.display_name,
    "model_info": f"{backend} / {model_name}",  # Extracted from translate_params
    "language": translate_params.get("language", ""),
    "device": translate_params.get("device", ""),
    "auto_translate": job.auto_translate,
    "auto_download": job.auto_download,
}
```

**Usage:**
- Called by `/api/jobs/{job_id}` endpoint (line 4550)
- Called by `_get_queue_status()` for running/queued jobs (lines 362, 366)

---

### 1.4 Worker Functions (Job State Updates)

#### _run_transcribe_worker()
**Location:** Lines 527-638

**State Updates:**
1. **Initial Setup** (lines 557-560):
   - `job.current_job` = job_dir.name
   - `job.current_prefix` = file_prefix
   - `job.add_log()` calls

2. **During Transcription** (lines 563-576):
   - Calls `_set_job_progress(job, status, t0)` in loop
   - Updates `job.plain_text` from partial segments

3. **Completion** (lines 619-630):
   ```python
   _set_job_progress(job, "✅ 原文识别完成...", t0, progress_pct=100, ...)
   job.plain_text = display_plain_text
   with _RUNTIME_LOCK:
       job.done = True
       job.running = False
   ```

4. **Error** (lines 632-637):
   ```python
   with _RUNTIME_LOCK:
       job.failed = True
       job.done = True
       job.running = False
   _set_job_progress(job, f"❌ 转录失败: {exc}", ...)
   job.add_log(f"[ERROR] {exc}")
   ```

---

#### _run_translate_worker()
**Location:** Lines 833-917

**State Updates:**
1. **Progress Callback** (lines 863-875):
   - Defined inside worker: `on_translate_progress(completed, total_count, eta)`
   - Called by `translate_segments()` function
   - Calls `_set_job_progress()` with calculated percentage

2. **Completion** (lines 900-910):
   ```python
   _set_job_progress(job, "✅ 翻译完成...", t0, progress_pct=100, ...)
   with _RUNTIME_LOCK:
       job.done = True
       job.running = False
   ```

3. **Error** (lines 912-917):
   ```python
   with _RUNTIME_LOCK:
       job.failed = True
       job.done = True
       job.running = False
   _set_job_progress(job, f"❌ 翻译失败: {exc}", ...)
   job.add_log(f"[ERROR] {exc}")
   ```

**Key Pattern:** All state updates are wrapped in `with _RUNTIME_LOCK:` blocks

---

### 1.5 API Endpoints

#### GET /api/jobs/{job_id}
**Location:** Lines 4547-4550

**Current Implementation:**
```python
@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str):
    job = _get_job(job_id)
    return _json_job(job)
```

**Usage:** Polled every 1 second by frontend

**Returns:** Full job state JSON

---

#### POST /api/jobs/{job_id}/stop
**Location:** Lines 4553-4557

```python
@app.post("/api/jobs/{job_id}/stop")
def api_job_stop(job_id: str):
    _ = _get_job(job_id)
    core.STOP_EVENT.set()
    return {"message": "stop requested"}
```

**Behavior:** Sets STOP_EVENT, workers check and exit gracefully

---

#### GET /api/queue/status
**Location:** Lines 4528-4530

```python
@app.get("/api/queue/status")
def api_queue_status():
    return _get_queue_status()
```

**Returns:**
- `transcribe_queue`: List of queued jobs
- `transcribe_count`: Queue length
- `running_job`: Currently running job (or null)
- `all_jobs`: Last 50 jobs (historical)

---

## 2. Frontend Polling Mechanism

### 2.1 startPoll() Function
**Location:** Lines 3410-3482 (in HTML/JavaScript)

**Signature:**
```javascript
function startPoll(){
    if(pollTimer) clearInterval(pollTimer);
    _prevJobState = { current_job:'', step_label:'', done:false };
    pollTimer = setInterval(async ()=>{
        // ... polling logic
    }, 1000);
}
```

**Behavior:**
1. Clears any existing timer
2. Resets `_prevJobState` to track changes
3. Starts interval at 1000ms (1 second)
4. Calls `/api/jobs/{currentJobId}` each iteration
5. Updates UI elements
6. Detects state changes and triggers actions

---

### 2.2 Polling Logic Breakdown

**Step 1: Fetch Job State**
```javascript
const data = await api('/api/jobs/'+currentJobId);
```

**Step 2: Update Basic UI**
```javascript
document.getElementById('statusText').value = data.status || '';
document.getElementById('plainText').value = data.plain_text || '';
document.getElementById('logText').value = data.log_text || '';
updateTaskPanel(data);
```

**Step 3: Detect State Changes**
```javascript
const jobChanged = data.current_job && data.current_job !== _prevJobState.current_job;
const stepChanged = data.step_label && data.step_label !== _prevJobState.step_label;
const justDone = data.done && !data.running && !_prevJobState.done;
```

**Step 4: React to Changes**
```javascript
if(jobChanged && data.current_job && data.current_prefix){
    // Refresh history when job directory is set
    const expectedWav = `workspace/${data.current_job}/${data.current_prefix}.wav`;
    refreshHistory(expectedWav);
}

if(stepChanged){
    // Refresh queue when step changes
    refreshQueueStatus();
}
```

**Step 5: Update Previous State**
```javascript
_prevJobState = {
    current_job: data.current_job || '',
    step_label: data.step_label || '',
    done: !!data.done,
};
```

---

### 2.3 justDone Detection and Auto-Actions

**Location:** Lines 3443-3479

**justDone Condition:**
```javascript
const justDone = data.done && !data.running && !_prevJobState.done;
```

**Logic Flow:**
1. **Task Completion Detected:**
   - Refresh history with job folder
   - Refresh queue status
   - Restore auto-translate/auto-download flags from server state

2. **Auto-Translate Path:**
   ```javascript
   if(pendingAutoFlags.translate && !data.failed){
       pendingAutoFlags.translate = false;
       clearInterval(pollTimer);  // Stop polling
       pollTimer = null;
       try{
           await startTranslate();
       }catch(e){
           console.error('自动翻译失败', e);
           startPoll();  // Resume polling on failure
       }
   }
   ```

3. **Auto-Download Path:**
   ```javascript
   else {
       clearInterval(pollTimer);  // Stop polling
       pollTimer = null;
       if(pendingAutoFlags.download && !data.failed){
           pendingAutoFlags.download = false;
           try{ await downloadOutputZip(); }catch(e){ console.error('自动下载失败', e); }
       }
   }
   ```

**Key Patterns:**
- Polling is stopped during auto-translate/auto-download
- Resumes if auto-translate fails
- Uses client-side `pendingAutoFlags` but falls back to server flags
- Prevents race conditions by clearing timer before async operations

---

### 2.4 Poll Timer Management

**Variables:**
```javascript
let pollTimer = null;  // Line 1672
let _prevJobState = { current_job:'', step_label:'', done:false };  // Line 1673
let pendingAutoFlags = { translate: false, download: false };  // Line 1674
```

**Timer Control:**
- **Start:** `startPoll()` called after starting transcribe/translate
- **Stop:** `clearInterval(pollTimer)` when job done or during auto-actions
- **Restart:** `startPoll()` called after auto-translate completes

**Entry Points:**
- Line 3340: After subtitle import completion
- Line 3387: After transcribe start
- Line 3407: After translate start
- Line 3545: On page load (resume running job)

---

## 3. Thread Safety and Concurrency

### 3.1 Lock Hierarchy
```
Runtime Lock (_RUNTIME_LOCK)
  ├── Protects: _RUNTIME_JOB
  ├── Protects: _ALL_JOBS
  ├── Protects: _TRANSCRIBE_QUEUE
  └── Protects: job state fields (done, running, failed)
```

### 3.2 Worker Thread Management
```
Main Thread (FastAPI)
  └── _RUNTIME_THREAD (daemon thread)
       └── Worker Function (_run_transcribe_worker / _run_translate_worker)
            └── Updates JobState (with _RUNTIME_LOCK)
```

### 3.3 State Update Pattern
```python
def _set_job_progress(job, status, start_ts, ...):
    # Direct field updates (no lock needed for in-progress job)
    job.status = decorated
    job.progress_pct = pct
    job.eta_seconds = eta
    job.step_label = step
    job.updated_at = time.time()

# Final state changes (with lock)
with _RUNTIME_LOCK:
    job.done = True
    job.running = False
```

---

## 4. Current SSE/Streaming Usage

**Location:** Lines 3655-3890

**Existing StreamingResponse Uses:**
1. Line 3666: `/api/folders/download-text` - ZIP download
2. Line 3703: `/api/folders/download-selected-text` - ZIP download
3. Line 3846: `/api/folders/download-multi` - ZIP download
4. Line 3890: `/api/folders/download-output` - ZIP download

**Pattern:** All use StreamingResponse for file downloads, NOT for SSE

**No SSE Implementation Found:**
- No `text/event-stream` content-type
- No `asyncio` generators with `yield`
- No SSE event format (`data: ...`)

---

## 5. Key Integration Points for SSE

### 5.1 Server-Side
1. **New Endpoint:** `GET /api/jobs/{job_id}/stream`
   - Replace polling for job status
   - Stream job state updates in real-time

2. **Event Types to Emit:**
   - `job_progress`: During transcription/translation
   - `job_log`: When logs are added
   - `job_complete`: When job finishes
   - `job_failed`: When job errors

3. **State Observation Pattern:**
   - Need to observe job state changes
   - Emit events on state transitions
   - Maintain connection until job completes

### 5.2 Client-Side
1. **Replace startPoll() with SSE connection:**
   ```javascript
   const eventSource = new EventSource(`/api/jobs/${jobId}/stream`);
   
   eventSource.addEventListener('job_progress', (e) => {
       const data = JSON.parse(e.data);
       updateTaskPanel(data);
   });
   
   eventSource.addEventListener('job_complete', (e) => {
       handleJobComplete(JSON.parse(e.data));
   });
   ```

2. **Preserve justDone logic:**
   - Detect completion from `job_complete` event
   - Trigger auto-translate/auto-download as before

3. **Handle connection states:**
   - `onopen`: Connection established
   - `onerror`: Connection failed/retry
   - `onclose`: Cleanup

---

## 6. Critical Considerations

### 6.1 Thread Safety
- SSE handler must hold `_RUNTIME_LOCK` when reading job state
- Worker threads must notify SSE connections of state changes
- Need mechanism for SSE observers to be notified on updates

### 6.2 Backward Compatibility
- Keep existing `/api/jobs/{job_id}` endpoint for non-SSE clients
- Allow graceful fallback to polling if SSE fails

### 6.3 Connection Management
- Multiple clients may observe same job (support multiple tabs)
- Clean up connections when jobs complete
- Handle client disconnects gracefully

### 6.4 State Change Notification
- Option 1: Observer pattern with job state listeners
- Option 2: Shared queue for SSE generators to poll
- Option 3: asyncio.Event for waking up SSE handlers

---

## 7. Summary of Key Locations

### Python Backend
- **JobState class:** Lines 119-146
- **Global state:** Lines 148-154
- **_set_job_progress():** Lines 456-487
- **_json_job():** Lines 307-344
- **_run_transcribe_worker():** Lines 527-638
- **_run_translate_worker():** Lines 833-917
- **_RUNTIME_LOCK usage:** Lines 148, 294, 303, 349, 373, 580, 628, 632, 821, 825, 908, 912, 3761, 4363, 4474, 4564, 4679
- **API endpoints:** Lines 4398-4737

### JavaScript Frontend
- **startPoll():** Lines 3410-3482
- **justDone detection:** Lines 3425, 3443-3479
- **Poll timer:** Line 1672
- **State tracking:** Lines 1673-1674

---

## 8. Recommended SSE Implementation Strategy

### Phase 1: Server-Side SSE Endpoint
1. Create async generator function that observes job state
2. Use `asyncio.Event` or shared queue for state notifications
3. Emit SSE events on state changes
4. Handle client disconnections

### Phase 2: State Observation Mechanism
1. Add observer list to JobState or use pub/sub
2. Modify `_set_job_progress()` to notify observers
3. Ensure thread-safe notification

### Phase 3: Client-Side Migration
1. Replace `startPoll()` with `EventSource` connection
2. Map SSE events to existing update functions
3. Preserve auto-translate/auto-download logic
4. Add error handling and fallback to polling

### Phase 4: Testing & Refinement
1. Test concurrent connections (multiple tabs)
2. Test rapid state changes
3. Test network interruption and reconnection
4. Verify backward compatibility

---

