const form = document.querySelector("#discovery-form");
const questionInput = document.querySelector("#question");
const modeInput = document.querySelector("#mode");
const discoverButton = document.querySelector("#discover-button");
const runtimeButton = document.querySelector("#load-runtime-button");
const feedback = document.querySelector("#feedback");
const statusBadge = document.querySelector("#status-badge");
const resultHeading = document.querySelector("#result-heading");
const resultSummary = document.querySelector("#result-summary");
const candidateList = document.querySelector("#candidate-list");
const detailHeading = document.querySelector("#detail-heading");
const detailContent = document.querySelector("#detail-content");
const serviceStatus = document.querySelector("#service-status");

let selectedDatasetId = null;
let lastDiscovery = null;

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

function setStatus(status) {
  const normalized = String(status || "READY").toLowerCase();
  statusBadge.textContent = String(status || "READY").replaceAll("_", " ");
  statusBadge.className = `status-badge ${normalized}`;
}

function renderSummary(discovery) {
  if (discovery.status === "CLARIFICATION_REQUIRED") {
    const metrics = discovery.ambiguity?.conflicting_metrics || [];
    const definitions = metrics.map((metric) => `<strong>${escapeHtml(metric.name)}</strong> (${escapeHtml(metric.domain)})`).join(" and ");
    resultSummary.className = "result-summary warning";
    resultSummary.innerHTML = `Clarification is required before selecting a dataset. ${definitions || "Multiple certified definitions are relevant"} are intentionally not interchangeable.`;
    return;
  }
  if (discovery.status === "RESOLVED") {
    resultSummary.className = "result-summary";
    const metric = discovery.resolved_metric;
    resultSummary.innerHTML = metric
      ? `Resolved to <strong>${escapeHtml(metric.name)}</strong>. The evidence below makes the selection inspectable.`
      : "A governed dataset match was resolved.";
    return;
  }
  resultSummary.className = "result-summary warning";
  resultSummary.textContent = "No governed dataset match was found. Try a more specific business term or metric definition.";
}

function renderCandidates(candidates) {
  candidateList.innerHTML = "";
  if (!candidates.length) return;
  for (const item of candidates) {
    const { candidate, dataset, runtime } = item;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `candidate-card${selectedDatasetId === dataset.id ? " selected" : ""}`;
    button.dataset.datasetId = dataset.id;
    const signals = candidate.reasons.map((reason) => `<span class="signal-pill">${escapeHtml(titleCase(reason.signal))}</span>`).join("");
    const runtimeText = runtime
      ? `<span class="signal-pill">${escapeHtml(titleCase(runtime.operational_health))} runtime</span>`
      : "";
    button.innerHTML = `
      <span class="candidate-title">
        <span><strong class="dataset-name">${escapeHtml(dataset.name)}</strong><span class="dataset-id">${escapeHtml(dataset.id)}</span></span>
        <span class="score">${escapeHtml(candidate.score)} pts</span>
      </span>
      <span class="candidate-description">${escapeHtml(dataset.description)}</span>
      <span class="signal-pills">${signals}${runtimeText}</span>
    `;
    button.addEventListener("click", () => selectDataset(dataset.id));
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
    return `<div class="detail-section"><h3>Operational trust</h3><p>No runtime observation is loaded for this dataset yet. Use <strong>Load runtime context</strong> to demonstrate freshness, quality, health, and usage signals.</p></div>`;
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

function renderDetail(context) {
  const { dataset, metrics, concepts, entities, runtime } = context;
  detailHeading.textContent = dataset.name;
  const metricItems = metrics.map((metric) => `<li>${escapeHtml(metric.name)}</li>`).join("") || "<li>No linked metrics</li>";
  const conceptItems = [...concepts, ...entities].map((item) => `<li>${escapeHtml(item.name)}</li>`).join("") || "<li>No linked concepts</li>";
  const candidate = lastDiscovery?.candidates.find((item) => item.dataset.id === dataset.id)?.candidate;
  const reasons = candidate?.reasons.map((reason) => `<li><strong>${escapeHtml(titleCase(reason.signal))}:</strong> ${escapeHtml(reason.detail)} <span class="good">+${escapeHtml(reason.points)}</span></li>`).join("") || "<li>Select a result from the latest discovery to view ranking evidence.</li>";
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

async function selectDataset(datasetId) {
  selectedDatasetId = datasetId;
  renderCandidates(lastDiscovery?.candidates || []);
  detailHeading.textContent = "Loading context…";
  detailContent.className = "empty-detail";
  detailContent.textContent = "Retrieving semantic and runtime context.";
  try {
    const context = await api(`/v1/datasets/${encodeURIComponent(datasetId)}/context`);
    renderDetail(context);
  } catch (error) {
    detailHeading.textContent = "Context unavailable";
    detailContent.textContent = error.message;
    setFeedback(error.message, true);
  }
}

async function discover() {
  const question = questionInput.value.trim();
  if (!question) return;
  discoverButton.disabled = true;
  discoverButton.textContent = "Discovering…";
  setFeedback("Running governed discovery…");
  try {
    const data = await api("/v1/discovery", {
      method: "POST",
      body: JSON.stringify({ question, mode: modeInput.value, limit: 5 }),
    });
    lastDiscovery = data;
    selectedDatasetId = null;
    setStatus(data.discovery.status);
    resultHeading.textContent = data.discovery.status === "CLARIFICATION_REQUIRED"
      ? "Clarification required"
      : data.discovery.status === "RESOLVED" ? "Dataset resolved" : "No governed match";
    renderSummary(data.discovery);
    renderCandidates(data.candidates);
    setFeedback(`${data.candidates.length} governed candidate${data.candidates.length === 1 ? "" : "s"} returned.`);
    if (data.candidates.length === 1) await selectDataset(data.candidates[0].dataset.id);
  } catch (error) {
    setFeedback(error.message, true);
  } finally {
    discoverButton.disabled = false;
    discoverButton.textContent = "Discover data";
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
    setFeedback("Runtime context loaded. Run Finance revenue discovery to see transparent reranking.");
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

form.addEventListener("submit", (event) => { event.preventDefault(); discover(); });
runtimeButton.addEventListener("click", loadRuntime);
document.querySelectorAll(".example").forEach((button) => button.addEventListener("click", () => {
  questionInput.value = button.dataset.question;
  questionInput.focus();
  discover();
}));

checkHealth();
