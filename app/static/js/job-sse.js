(function () {
  const el = document.getElementById("jobStatusRoot");
  if (!el) return;

  const jobId = el.dataset.jobId;
  const eventsUrl = `/tools/job/${jobId}/events`;
  const statusUrl = `/tools/job/${jobId}/status.json`;

  const bar = document.getElementById("jobProgressBar");
  const pill = document.getElementById("jobStatusPill");
  const msg = document.getElementById("jobMessage");
  const downloadWrap = document.getElementById("jobDownloadWrap");
  const stepper = document.getElementById("jobStepper");
  const retryBtn = document.getElementById("jobRetryBtn");

  let pollingActive = false;

  function render(data) {
    if (bar) {
      bar.style.width = (data.progress || 0) + "%";
      bar.setAttribute("aria-valuenow", data.progress || 0);
    }
    if (msg) msg.textContent = data.message || "";
    if (pill) {
      pill.textContent = data.status;
      pill.className = "status-pill " + data.status;
    }
    const track = bar ? bar.closest(".progress-snpro") : null;
    if (track) track.classList.toggle("is-active", data.status === "pending" || data.status === "running");

    if (data.status === "success" && data.has_result && downloadWrap) {
      downloadWrap.classList.remove("d-none");
    }
    if (data.status === "failure" && retryBtn) {
      retryBtn.classList.remove("d-none");
    }

    if (stepper && data.stage) {
      const steps = stepper.querySelectorAll(".step-item");
      steps.forEach(step => {
        const stepName = step.dataset.stage;
        if (stepName === data.stage) {
          step.className = "step-item active";
        } else {
          step.className = "step-item";
        }
      });
    }

    return data.status === "success" || data.status === "failure";
  }

  function startPolling() {
    if (pollingActive) return;
    pollingActive = true;
    async function poll() {
      try {
        const res = await fetch(statusUrl);
        if (res.status === 429) {
          setTimeout(poll, 10000);
          return;
        }
        const data = await res.json();
        const done = render(data);
        if (!done) setTimeout(poll, 5000);
      } catch (e) {
        setTimeout(poll, 5000);
      }
    }
    poll();
  }

  let errorCount = 0;
  const es = new EventSource(eventsUrl);

  es.onmessage = function (event) {
    try {
      const data = JSON.parse(event.data);
      const done = render(data);
      if (done) {
        es.close();
      } else if (data.message && data.message.includes("Stream lifetime cap reached")) {
        // Reconnects caused by the cap must not count toward the 3-error fallback
        es.close();
        startPolling();
      }
    } catch (e) {}
  };

  es.onerror = function () {
    if (es.readyState === EventSource.CLOSED) {
      es.close();
      startPolling();
      return;
    }
    errorCount++;
    if (errorCount >= 3) {
      es.close();
      startPolling();
    }
  };
})();
