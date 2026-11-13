(function () {
  "use strict";

  var STATE_LABELS = {
    pending: "已分配，待连接",
    established: "已建立",
    expired: "已过期",
    unknown: "未知"
  };

  function setFeedback(element, message, isError) {
    if (!element) {
      return;
    }
    element.textContent = message;
    if (isError) {
      element.classList.add("is-error");
    } else {
      element.classList.remove("is-error");
    }
  }

  function fallbackCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "readonly");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (error) {
      copied = false;
    }
    document.body.removeChild(area);
    return copied;
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (error) {
        return fallbackCopy(text);
      }
    }
    return fallbackCopy(text);
  }

  function selectElementText(element) {
    var selection = window.getSelection ? window.getSelection() : null;
    if (!selection) {
      return;
    }
    var range = document.createRange();
    range.selectNodeContents(element);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var target = document.getElementById(button.getAttribute("data-copy-target"));
      var feedback = document.getElementById(button.getAttribute("data-copy-feedback"));
      if (!target) {
        setFeedback(feedback, "复制失败：找不到要复制的内容。", true);
        return;
      }
      var original = button.textContent;
      button.disabled = true;
      button.textContent = "复制中";
      setFeedback(feedback, "", false);
      var ok = await copyText(target.textContent);

      if (ok) {
        button.textContent = "已复制";
        setFeedback(feedback, "已复制到剪贴板。", false);
      } else {
        selectElementText(target);
        button.textContent = original;
        setFeedback(feedback, "自动复制失败，已选中文本，请按 Ctrl+C 手动复制。", true);
      }
      window.setTimeout(function () {
        button.textContent = original;
        button.disabled = false;
      }, ok ? 1600 : 0);
    });
  });

  function formatRemaining(totalSeconds) {
    var seconds = Math.max(0, Math.ceil(totalSeconds));
    var days = Math.floor(seconds / 86400);
    var hours = Math.floor((seconds % 86400) / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    function pad(value) {
      return String(value).padStart(2, "0");
    }
    if (days > 0) {
      return days + " 天 " + pad(hours) + ":" + pad(minutes) + ":" + pad(secs);
    }
    if (hours > 0) {
      return pad(hours) + ":" + pad(minutes) + ":" + pad(secs);
    }
    return pad(minutes) + ":" + pad(secs);
  }

  function formatLocalTime(isoValue) {
    if (!isoValue) {
      return "-";
    }
    var date = new Date(isoValue);
    if (Number.isNaN(date.getTime())) {
      return "-";
    }
    return date.toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    });
  }

  var panel = document.querySelector("[data-tunnel-result]");
  if (panel) {
    var statusUrl = panel.getAttribute("data-status-url");
    var pollSeconds = Math.max(5, Number(panel.getAttribute("data-poll-seconds")) || 5);
    var pollInterval = pollSeconds * 1000;
    var badgeContainer = panel.querySelector("[data-status-badge]");
    var badge = badgeContainer ? badgeContainer.querySelector(".badge") : null;
    var remainingElement = panel.querySelector("[data-remaining]");
    var checkedElement = panel.querySelector("[data-checked-at]");
    var actualElement = panel.querySelector("[data-actual-listen]");
    var statusMessage = panel.querySelector("[data-status-message]");
    var refreshButton = panel.querySelector("[data-refresh-status]");
    var state = panel.getAttribute("data-state") || "unknown";
    var expiresAt = Date.parse(panel.getAttribute("data-expires-at") || "");
    var stopped = state === "expired" || state === "unknown";
    var inFlight = false;
    var pollTimer = null;

    function isTerminal(value) {
      return value === "expired" || value === "unknown";
    }

    function renderBadge(nextState, label) {
      if (!badge) {
        return;
      }
      badge.className = "badge badge-" + nextState;
      badge.textContent = label || STATE_LABELS[nextState] || "未知";
    }

    function renderRemaining() {
      if (!remainingElement) {
        return;
      }
      if (state === "expired") {
        remainingElement.textContent = "剩余 00:00";
        return;
      }
      if (Number.isNaN(expiresAt)) {
        return;
      }
      remainingElement.textContent = "剩余 " + formatRemaining((expiresAt - Date.now()) / 1000);
    }

    function clearPollTimer() {
      if (pollTimer !== null) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function schedulePoll() {
      clearPollTimer();
      if (stopped || document.visibilityState !== "visible") {
        return;
      }
      pollTimer = window.setTimeout(function () {
        refreshStatus(false);
      }, pollInterval);
    }

    function applyStatus(data) {
      state = data.state || "unknown";
      stopped = isTerminal(state);
      renderBadge(state, data.state_label);
      var serverExpiry = Date.parse(data.expires_at || "");
      if (!Number.isNaN(serverExpiry)) {
        expiresAt = serverExpiry;
      } else if (typeof data.remaining_seconds === "number" && !Number.isNaN(expiresAt)) {
        expiresAt = Date.now() + data.remaining_seconds * 1000;
      }
      renderRemaining();

      if (checkedElement) {
        checkedElement.textContent = formatLocalTime(data.checked_at);
        if (data.checked_at) {
          checkedElement.setAttribute("data-iso", data.checked_at);
        }
      }
      if (actualElement) {
        var addresses = Array.isArray(data.actual_listen_addresses)
          ? data.actual_listen_addresses
          : [];
        if (addresses.length > 0) {
          actualElement.textContent = addresses.join(", ");
        } else if (state === "expired") {
          actualElement.textContent = "端口已不再监听";
        } else {
          actualElement.textContent = "尚未监听";
        }
      }

      if (state === "expired") {
        setFeedback(statusMessage, "隧道已过期或端口已不再监听，端口已回收。", false);
        if (refreshButton) {
          refreshButton.disabled = true;
        }
      } else if (state === "unknown") {
        setFeedback(
          statusMessage,
          "无法读取宿主机端口状态，自动轮询已停止。可手动刷新重试。",
          true
        );
        if (refreshButton) {
          refreshButton.disabled = false;
        }
      } else if (state === "established") {
        setFeedback(statusMessage, "隧道已建立，服务器端口正在监听。", false);
        if (refreshButton) {
          refreshButton.disabled = false;
        }
      } else {
        setFeedback(statusMessage, "", false);
        if (refreshButton) {
          refreshButton.disabled = false;
        }
      }
      schedulePoll();
    }

    async function refreshStatus(manual) {
      if (inFlight) {
        return;
      }
      if (!manual && (stopped || document.visibilityState !== "visible")) {
        return;
      }
      inFlight = true;
      clearPollTimer();
      var originalLabel = refreshButton ? refreshButton.textContent : "";
      if (refreshButton) {
        refreshButton.disabled = true;
        refreshButton.textContent = "检查中";
      }
      try {
        var response = await fetch(statusUrl, {
          method: "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers: { "Accept": "application/json" }
        });
        if (response.status === 401) {
          stopped = true;
          clearPollTimer();
          setFeedback(statusMessage, "登录会话已过期，请重新登录。", true);
          window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname));
          return;
        }
        if (response.status === 404) {
          stopped = true;
          clearPollTimer();
          renderBadge("expired", "已过期");
          if (remainingElement) {
            remainingElement.textContent = "剩余 00:00";
          }
          setFeedback(statusMessage, "隧道不存在或已经被回收。", true);
          if (refreshButton) {
            refreshButton.disabled = true;
          }
          return;
        }
        var data = null;
        try {
          data = await response.json();
        } catch (error) {
          data = null;
        }
        if (!response.ok) {
          var reason = data && data.error && data.error.message
            ? data.error.message
            : "服务器返回 " + response.status;
          throw new Error(reason);
        }
        if (!data) {
          throw new Error("服务器响应格式无效");
        }
        applyStatus(data);
      } catch (error) {
        setFeedback(
          statusMessage,
          "状态刷新失败，已保留上一次已知状态；将在 " + pollSeconds + " 秒后重试。",
          true
        );
        if (!stopped) {
          schedulePoll();
        }
      } finally {
        inFlight = false;
        if (refreshButton) {
          refreshButton.textContent = originalLabel;
          if (state !== "expired") {
            refreshButton.disabled = false;
          }
        }
      }
    }

    if (refreshButton) {
      refreshButton.addEventListener("click", function () {
        refreshStatus(true);
      });
    }

    window.setInterval(renderRemaining, 1000);
    renderRemaining();
    schedulePoll();

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") {
        if (!stopped) {
          refreshStatus(false);
        }
      } else {
        clearPollTimer();
      }
    });
  }

  var modeRadios = document.querySelectorAll("[data-policy-mode]");
  if (modeRadios.length > 0) {
    var specificWrapper = document.querySelector("[data-specific-wrapper]");
    var specificInput = document.querySelector("[data-specific-input]");
    var allRisk = document.querySelector("[data-all-risk]");

    var updatePolicyFields = function () {
      var selected = document.querySelector("[data-policy-mode]:checked");
      var mode = selected ? selected.value : "";
      if (specificWrapper) {
        specificWrapper.hidden = mode !== "specific";
      }
      if (specificInput) {
        specificInput.required = mode === "specific";
      }
      if (allRisk) {
        allRisk.hidden = mode !== "all";
      }
    };

    modeRadios.forEach(function (radio) {
      radio.addEventListener("change", updatePolicyFields);
    });
    updatePolicyFields();
  }

  document.querySelectorAll("[data-delete-form]").forEach(function (form) {
    var arm = form.querySelector("[data-delete-arm]");
    var confirmation = form.querySelector("[data-delete-confirm]");
    var cancel = form.querySelector("[data-delete-cancel]");
    if (!arm || !confirmation || !cancel) {
      return;
    }
    arm.addEventListener("click", function () {
      confirmation.hidden = false;
      arm.hidden = true;
      var submit = confirmation.querySelector('button[type="submit"]');
      if (submit) {
        submit.focus();
      }
    });
    cancel.addEventListener("click", function () {
      confirmation.hidden = true;
      arm.hidden = false;
      arm.focus();
    });
  });
})();
