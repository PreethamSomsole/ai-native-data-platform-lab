const chatForm = document.querySelector("#chat-form");
const questionInput = document.querySelector("#question");
const sendButton = document.querySelector("#send-button");
const chatHistory = document.querySelector("#chat-history");
const candidateList = document.querySelector("#candidate-list");
const detailHeading = document.querySelector("#detail-heading");
const detailContent = document.querySelector("#detail-content");
const serviceStatus = document.querySelector("#service-status");
const statusBadge = document.querySelector("#status-badge");
const runtimeButton = document.querySelector("#load-runtime-button");
const feedback = document.querySelector("#feedback");

let selectedDatasetId = null;
let currentCandidates = [];
let lastDiscoveryMode = "hybrid";

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const titleCase = (value) => String(value ?? "unknown").replaceAll("_", " ")
  .replace(/\b\w/g, (letter) => letter.toUpperCase());

const formatNumber = (value) => value == null ? "—" : new Intl.NumberFormat().format(value);

function setFeedback(message, error = false) {
  feedback.textContent = message;
  feedback.classList.toggle("error", error);
}

function setStatus(status) {
  const normalized = String(status || "READY").toLowerCase();
  statusBadge.textContent = String(status || "READY").replaceAll("_", " ");
  statusBadge.className = `status-badge ${normalized}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response.json();
}

function addMessage(role, text) {
  const msgDiv = document.createElement("div");
  msgDiv.className = `chat-message ${role}`;
  msgDiv.innerHTML = `<div class="message-content">${escapeHtml(text)}</div>`;
  chatHistory.appendChild(msgDiv);
  chatHistory.scrollTop = chatHistory.scrollHeight;
}

function renderCandidates(candidates) {
  candidateList.innerHTML = "";
  if (!candidates || !candidates.length) return;
  for (const candidate of candidates) {
    // Determine dataset from either direct object or nested discovery candidate
    const dataset = candidate.dataset || candidate; 
    const score = candidate.score || (candidate.candidate && candidate.candidate.score) || 0;
    const reasons = candidate.reasons || (candidate.candidate && candidate.candidate.reasons) || [];
    
    const button = document.createElement("div");
    button.className = `candidate-card${selectedDatasetId === dataset.id ? " selected" : ""}`;
    button.dataset.datasetId = dataset.id;
    
    const signals = reasons.map((reason) => `<span class="signal-pill">${escapeHtml(titleCase(reason.signal))}</span>`).join("");
    
    button.innerHTML = `
      <span class="candidate-title">
        <span><strong class="dataset-name">${escapeHtml(dataset.name)}</strong><span class="dataset-id">${escapeHtml(dataset.id)}</span></span>
        ${score ? `<span class="score">${escapeHtml(score)} pts</span>` : ''}
      </span>
      <span class="candidate-description">${escapeHtml(dataset.description)}</span>
      <span class="signal-pills">${signals}</span>
    `;
    button.addEventListener("click", () => selectDataset(dataset.id, candidates));
    candidateList.append(button);
  }
}

function statusClass(value) {
  if (["fresh", "passing", "healthy"].includes(value)) return "good";
  if (["stale", "warning", "degraded"].includes(value)) return "caution";
  if (["failing", "failed"].includes(value)) return "bad";
  return "";
}

function runtimeMarkup(runtime) {
  if (!runtime) {
    return `<div class="detail-section"><h3>Operational trust</h3><p>No runtime observation is loaded for this dataset yet.</p></div>`;
  }
  return `
    <div class="detail-section">
      <h3>Operational trust</h3>
      <div class="runtime-grid">
        <div class="runtime-tile"><span class="runtime-label">Freshness</span><span class="runtime-value ${statusClass(runtime.freshness_status)}">${escapeHtml(titleCase(runtime.freshness_status))}</span></div>
        <div class="runtime-tile"><span class="runtime-label">Quality</span><span class="runtime-value ${statusClass(runtime.quality_status)}">${escapeHtml(titleCase(runtime.quality_status))}</span></div>
        <div class="runtime-tile"><span class="runtime-label">Pipeline health</span><span class="runtime-value ${statusClass(runtime.operational_health)}">${escapeHtml(titleCase(runtime.operational_health))}</span></div>
        <div class="runtime-tile"><span class="runtime-label">30-day usage</span><span class="runtime-value">${formatNumber(runtime.usage_count_30d)}</span></div>
        <div class="runtime-tile"><span class="runtime-label">Row count</span><span class="runtime-value">${formatNumber(runtime.row_count)}</span></div>
        <div class="runtime-tile"><span class="runtime-label">Quality checks</span><span class="runtime-value">${formatNumber(runtime.quality_checks_passed)} passed</span></div>
      </div>
    </div>`;
}

function renderDetail(context, candidateInfo) {
  const { dataset, metrics, concepts, entities, runtime } = context;
  detailHeading.textContent = dataset.name;
  const metricItems = metrics.map((metric) => `<li>${escapeHtml(metric.name)}</li>`).join("") || "<li>No linked metrics</li>";
  const conceptItems = [...concepts, ...entities].map((item) => `<li>${escapeHtml(item.name)}</li>`).join("") || "<li>No linked concepts</li>";
  
  let reasons = "<li>No ranking evidence available.</li>";
  if (candidateInfo) {
      const evidenceList = candidateInfo.reasons || (candidateInfo.candidate && candidateInfo.candidate.reasons) || [];
      if (evidenceList.length > 0) {
          reasons = evidenceList.map((reason) => `<li><strong>${escapeHtml(titleCase(reason.signal))}:</strong> ${escapeHtml(reason.detail)} <span class="good">+${escapeHtml(reason.points)}</span></li>`).join("");
      }
  }

  detailContent.className = "";
  detailContent.innerHTML = `
    <div class="detail-section"><h3>Business definition</h3><p>${escapeHtml(dataset.description)}</p></div>
    <div class="detail-section"><h3>Governance</h3><div class="definition-list"><li>${escapeHtml(titleCase(dataset.certification))}</li><li>${escapeHtml(dataset.layer)} layer</li><li>Owner: ${escapeHtml(dataset.owner)}</li><li>${escapeHtml(dataset.refresh_cadence)}</li></div></div>
    <div class="detail-section"><h3>Canonical metrics</h3><ul class="definition-list">${metricItems}</ul></div>
    <div class="detail-section"><h3>Entities & concepts</h3><ul class="definition-list">${conceptItems}</ul></div>
    ${runtimeMarkup(runtime)}
    <div class="detail-section"><h3>Why this rank</h3><ul class="reason-list">${reasons}</ul></div>
  `;
}

async function selectDataset(datasetId, candidates) {
  selectedDatasetId = datasetId;
  renderCandidates(candidates);
  detailHeading.textContent = "Loading context…";
  detailContent.className = "empty-detail";
  detailContent.textContent = "Retrieving semantic and runtime context.";
  try {
    const context = await api(`/v1/datasets/${encodeURIComponent(datasetId)}/context`);
    let cInfo = candidates.find(c => (c.dataset && c.dataset.id === datasetId) || (c.id === datasetId));
    renderDetail(context, cInfo);
  } catch (error) {
    detailHeading.textContent = "Context unavailable";
    detailContent.textContent = error.message;
    setFeedback(error.message, true);
  }
}

async function askAgent() {
  const question = questionInput.value.trim();
  if (!question) return;
  
  questionInput.value = "";
  sendButton.disabled = true;
  addMessage("user", question);
  
  // Clear evidence pane
  candidateList.innerHTML = "";
  detailHeading.textContent = "Discovering...";
  detailContent.innerHTML = "Consulting semantic rules and metadata...";
  setStatus("WORKING");

  try {
    // Try reasoning endpoint first
    let isReasoning = true;
    let data;
    try {
        data = await api("/v1/reasoning/dataset-selection", {
            method: "POST",
            body: JSON.stringify({ question, mode: lastDiscoveryMode, limit: 5 }),
        });
    } catch(err) {
        if (err.message.includes("503") || err.message.includes("reasoning provider is not configured")) {
            isReasoning = false;
        } else {
            throw err;
        }
    }

    if (isReasoning && data) {
        setStatus(data.status);
        let explanation = data.explanation;
        if (data.status === "CLARIFICATION_REQUIRED" && data.clarification_question) {
            explanation += "\n\n" + data.clarification_question;
        }
        addMessage("agent", explanation);
        
        // We need the full contexts to show the right pane properly
        if (data.candidate_ids && data.candidate_ids.length > 0) {
            detailHeading.textContent = "Fetching context...";
            
            // Fetch discovery payload just to get the rich candidate objects for the UI
            const discData = await api("/v1/discovery", {
                method: "POST",
                body: JSON.stringify({ question, mode: lastDiscoveryMode, limit: 5 }),
            });
            currentCandidates = discData.candidates;
            
            if (data.selected_dataset_id) {
                await selectDataset(data.selected_dataset_id, currentCandidates);
            } else if (currentCandidates.length > 0) {
                await selectDataset(currentCandidates[0].dataset.id, currentCandidates);
            }
        } else {
            detailHeading.textContent = "No Candidates";
            detailContent.innerHTML = "The deterministic engine did not return any valid datasets.";
        }
    } else {
        // Fallback to Discovery
        const discData = await api("/v1/discovery", {
            method: "POST",
            body: JSON.stringify({ question, mode: lastDiscoveryMode, limit: 5 }),
        });
        
        setStatus(discData.discovery.status);
        currentCandidates = discData.candidates;
        
        // Mock Agent Message
        if (discData.discovery.status === "CLARIFICATION_REQUIRED") {
            const metrics = discData.discovery.ambiguity?.conflicting_metrics || [];
            const definitions = metrics.map((metric) => `${metric.name} (${metric.domain})`).join(" and ");
            addMessage("agent", `I found multiple certified definitions that could match. Clarification is required because ${definitions || "these"} are intentionally not interchangeable.`);
        } else if (discData.discovery.status === "RESOLVED") {
            addMessage("agent", `I resolved your request to ${discData.discovery.resolved_metric?.name || "a governed dataset"}. I've loaded the context and evidence in the right panel.`);
        } else {
            addMessage("agent", "I could not find a governed match for that question. Please try a more specific business term.");
        }
        
        if (currentCandidates.length > 0) {
            await selectDataset(currentCandidates[0].dataset.id, currentCandidates);
        } else {
            detailHeading.textContent = "No Match";
            detailContent.innerHTML = "";
        }
    }

  } catch (error) {
    addMessage("agent", `Error: ${error.message}`);
    setStatus("ERROR");
  } finally {
    sendButton.disabled = false;
    questionInput.focus();
  }
}

async function loadRuntime() {
  runtimeButton.disabled = true;
  runtimeButton.textContent = "Loading…";
  const observedAt = new Date();
  const successfulAt = new Date(observedAt.getTime() - 5 * 60 * 1000);
  try {
    await api("/v1/runtime/observations", {
      method: "POST",
      body: JSON.stringify({
        dataset_id: "gold.finance_revenue",
        observed_at: observedAt.toISOString(),
        source: "demo-finance-revenue-pipeline",
        last_successful_run_at: successfulAt.toISOString(),
        freshness_status: "fresh",
        quality_status: "passing",
        quality_checks_passed: 12,
        quality_checks_failed: 0,
        row_count: 1250000,
        usage_count_30d: 140,
        operational_health: "healthy",
      }),
    });
    setFeedback("Runtime context loaded.");
    runtimeButton.textContent = "Runtime context loaded";
  } catch (error) {
    setFeedback(error.message, true);
    runtimeButton.textContent = "Load runtime context";
  } finally {
    runtimeButton.disabled = false;
  }
}

async function checkHealth() {
  try {
    const health = await api("/health");
    serviceStatus.className = "service-status ok";
    serviceStatus.lastChild.textContent = health.status === "ok" ? "Service ready" : "Service degraded";
  } catch {
    serviceStatus.className = "service-status error";
    serviceStatus.lastChild.textContent = "Service unavailable";
  }
}

chatForm.addEventListener("submit", (event) => { event.preventDefault(); askAgent(); });
runtimeButton.addEventListener("click", loadRuntime);
document.querySelectorAll(".example").forEach((button) => button.addEventListener("click", () => {
  questionInput.value = button.dataset.question;
  askAgent();
}));

checkHealth();
