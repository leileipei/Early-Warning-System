document.addEventListener("DOMContentLoaded", () => {
  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  const formPayload = (form, fields) => {
    const payload = new FormData();
    for (const [name, value] of fields) {
      payload.append(name, value?.value || "");
    }
    return payload;
  };

  const fetchSqlResult = async (endpoint, payload) => {
    const response = await fetch(endpoint, {
      method: "POST",
      body: payload,
      headers: { Accept: "application/json" },
    });
    return { response, data: await response.json() };
  };

  const validateSql = async (button) => {
    const form = button.closest("form");
    const endpoint = button.dataset.endpoint;
    const sqlInput = form?.querySelector("[data-sql-input]");
    const dataSourceInput = form?.querySelector('[name="data_source_id"]');
    const result = form?.querySelector("[data-sql-check-result]");
    if (!form || !endpoint || !sqlInput || !result) {
      return;
    }

    button.disabled = true;
    result.textContent = "检测中...";
    result.classList.remove("is-success", "is-error");

    try {
      const payload = formPayload(form, [
        ["sql_text", sqlInput],
        ["data_source_id", dataSourceInput],
      ]);
      const csrfInput = form.querySelector('[name="_csrf_token"]');
      payload.append("_csrf_token", csrfInput?.value || "");
      const { response, data } = await fetchSqlResult(
        endpoint,
        payload,
      );
      result.textContent = data.message || "SQL 检测失败";
      result.classList.toggle("is-success", response.ok);
      result.classList.toggle("is-error", !response.ok);
    } catch (_error) {
      result.textContent = "SQL 检测失败，请稍后重试";
      result.classList.add("is-error");
    } finally {
      button.disabled = false;
    }
  };

  const previewSql = async (button) => {
    const form = button.closest("form");
    const endpoint = button.dataset.endpoint;
    const sqlInput = form?.querySelector("[data-sql-input]");
    const dataSourceInput = form?.querySelector('[name="data_source_id"]');
    const timeoutInput = form?.querySelector('[name="query_timeout_seconds"]');
    const result = form?.querySelector("[data-sql-preview-result]");
    if (!form || !endpoint || !sqlInput || !result) {
      return;
    }

    button.disabled = true;
    result.className = "preview-panel";
    result.textContent = "预览中...";

    try {
      const payload = formPayload(form, [
        ["sql_text", sqlInput],
        ["data_source_id", dataSourceInput],
        ["query_timeout_seconds", timeoutInput],
      ]);
      const csrfInput = form.querySelector('[name="_csrf_token"]');
      payload.append("_csrf_token", csrfInput?.value || "");
      const { response, data } = await fetchSqlResult(
        endpoint,
        payload,
      );
      if (!response.ok) {
        result.classList.add("is-error");
        result.textContent = data.message || "预览失败";
        return;
      }

      result.classList.add("is-success");
      if (!data.rows?.length) {
        result.textContent = data.message || "查询成功，暂无结果";
        return;
      }

      const headers = data.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
      const rows = data.rows
        .map((row) => {
          const cells = data.columns
            .map((column) => `<td>${escapeHtml(row[column])}</td>`)
            .join("");
          return `<tr>${cells}</tr>`;
        })
        .join("");
      result.innerHTML = `
        <p>${escapeHtml(data.message || "查询成功")}</p>
        <div class="table-shell">
          <table class="preview-table">
            <thead><tr>${headers}</tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    } catch (_error) {
      result.classList.add("is-error");
      result.textContent = "预览失败，请稍后重试";
    } finally {
      button.disabled = false;
    }
  };

  /* ============ 主题切换 ============ */
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      if (window.ewsTheme) {
        window.ewsTheme.toggle();
      }
    });
  });

  /* ============ 侧边栏（移动端抽屉） ============ */
  const sidebar = document.querySelector("[data-sidebar]");
  const backdrop = document.querySelector("[data-sidebar-backdrop]");
  const setSidebarOpen = (open) => {
    if (!sidebar) {
      return;
    }
    sidebar.classList.toggle("is-open", open);
    backdrop?.classList.toggle("is-visible", open);
    document
      .querySelectorAll("[data-sidebar-toggle]")
      .forEach((button) => button.setAttribute("aria-expanded", String(open)));
  };

  document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      setSidebarOpen(!sidebar?.classList.contains("is-open"));
    });
  });
  backdrop?.addEventListener("click", () => setSidebarOpen(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setSidebarOpen(false);
    }
  });

  /* ============ 规则列表前端过滤 ============ */
  const ruleFilterInput = document.querySelector("[data-rule-filter]");
  if (ruleFilterInput) {
    const ruleRows = Array.from(document.querySelectorAll("[data-rule-row]"));
    const ruleEmptyRow = document.querySelector("[data-rule-empty]");
    ruleFilterInput.addEventListener("input", () => {
      const keyword = ruleFilterInput.value.trim().toLowerCase();
      let visibleCount = 0;
      ruleRows.forEach((row) => {
        const haystack = `${row.dataset.name || ""} ${row.dataset.cron || ""}`.toLowerCase();
        const matched = !keyword || haystack.includes(keyword);
        row.hidden = !matched;
        if (matched) {
          visibleCount += 1;
        }
      });
      if (ruleEmptyRow) {
        ruleEmptyRow.hidden = visibleCount !== 0;
      }
    });
  }

  /* ============ 配置页页签 ============ */
  const settingsTabButtons = document.querySelectorAll("[data-settings-tab]");
  if (settingsTabButtons.length) {
    const settingsPanels = document.querySelectorAll("[data-settings-panel]");
    const tabStorageKey = "settings-tab";
    const validTabs = ["sql", "smtp", "security"];

    const activateSettingsTab = (name, persist) => {
      const target = validTabs.includes(name) ? name : "sql";
      settingsTabButtons.forEach((button) => {
        const isActive = button.dataset.settingsTab === target;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-selected", String(isActive));
      });
      settingsPanels.forEach((panel) => {
        panel.hidden = panel.dataset.settingsPanel !== target;
      });
      if (persist) {
        try {
          window.sessionStorage.setItem(tabStorageKey, target);
        } catch (_error) {
          /* 无法持久化时仅切换当前页 */
        }
      }
    };

    settingsTabButtons.forEach((button) => {
      button.addEventListener("click", () => activateSettingsTab(button.dataset.settingsTab, true));
    });
    /* 表单提交前记住所在页签，提交重定向回来后恢复 */
    settingsPanels.forEach((panel) => {
      panel.querySelectorAll("form").forEach((form) => {
        form.addEventListener("submit", () => {
          try {
            window.sessionStorage.setItem(tabStorageKey, panel.dataset.settingsPanel);
          } catch (_error) {
            /* 无法持久化时保持默认页签 */
          }
        });
      });
    });

    let initialTab = null;
    try {
      initialTab = window.sessionStorage.getItem(tabStorageKey);
    } catch (_error) {
      initialTab = null;
    }
    const tabParam = new URLSearchParams(window.location.search).get("tab");
    activateSettingsTab(tabParam || initialTab || "sql", false);
  }

  /* ============ 成功提示自动消退 ============ */
  document.querySelectorAll(".form-success").forEach((notice) => {
    window.setTimeout(() => {
      notice.classList.add("is-dismissing");
      notice.addEventListener("transitionend", () => notice.remove(), { once: true });
    }, 4500);
  });

  /* ============ 确认模态框 ============ */
  const openConfirmModal = (message, onConfirm) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";

    const dialog = document.createElement("div");
    dialog.className = "modal";
    dialog.setAttribute("role", "alertdialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h2");
    title.textContent = "确认操作";

    const body = document.createElement("p");
    body.textContent = message;

    const actions = document.createElement("div");
    actions.className = "modal-actions";

    const cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.className = "button button-secondary";
    cancelButton.textContent = "取消";

    const confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.className = "button button-danger";
    confirmButton.textContent = "确认";

    const close = () => overlay.remove();
    cancelButton.addEventListener("click", close);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) {
        close();
      }
    });
    document.addEventListener("keydown", function onEscape(event) {
      if (event.key === "Escape") {
        close();
        document.removeEventListener("keydown", onEscape);
      }
    });
    confirmButton.addEventListener("click", () => {
      close();
      onConfirm();
    });

    actions.append(cancelButton, confirmButton);
    dialog.append(title, body, actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    confirmButton.focus();
  };

  /* ============ 表单提交拦截 ============ */
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) {
      return;
    }
    event.preventDefault();
    openConfirmModal(form.dataset.confirm, () => {
      form.submit();
    });
  });

  /* ============ SQL 检测 / 预览按钮 ============ */
  document.addEventListener("click", async (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }
    const button = event.target.closest("[data-sql-check-button], [data-sql-preview-button]");
    if (!(button instanceof HTMLButtonElement)) {
      return;
    }
    if (button.matches("[data-sql-check-button]")) {
      await validateSql(button);
    } else {
      await previewSql(button);
    }
  });
});
