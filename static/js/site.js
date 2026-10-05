document.addEventListener("DOMContentLoaded", () => {
    const accountMenu = document.querySelector(".account-menu");
    const accountMenuTrigger = document.getElementById("account-menu-trigger");
    const accountMenuPanel = document.getElementById("account-menu-panel");
    if (accountMenu && accountMenuTrigger && accountMenuPanel) {
        const mobileProfileLayout = window.matchMedia("(max-width: 700px)");
        const profileLink = accountMenuPanel.querySelector('a[href="/Account/Profile"]');
        const syncProfileLayout = () => {
            document.body.classList.toggle(
                "profile-menu-open",
                !mobileProfileLayout.matches && accountMenuTrigger.getAttribute("aria-expanded") === "true"
            );
            if (profileLink) {
                if (mobileProfileLayout.matches) {
                    profileLink.target = "_blank";
                    profileLink.rel = "noopener noreferrer";
                } else {
                    profileLink.removeAttribute("target");
                    profileLink.removeAttribute("rel");
                }
            }
        };

        const closeAccountMenu = (returnFocus = false) => {
            accountMenuPanel.hidden = true;
            accountMenuTrigger.setAttribute("aria-expanded", "false");
            syncProfileLayout();
            if (returnFocus) accountMenuTrigger.focus();
        };

        accountMenuTrigger.addEventListener("click", () => {
            const isOpen = accountMenuTrigger.getAttribute("aria-expanded") === "true";
            accountMenuPanel.hidden = isOpen;
            accountMenuTrigger.setAttribute("aria-expanded", String(!isOpen));
            syncProfileLayout();
            if (!isOpen) accountMenuPanel.querySelector("a")?.focus();
        });

        mobileProfileLayout.addEventListener("change", syncProfileLayout);
        syncProfileLayout();

        document.addEventListener("click", event => {
            if (!accountMenu.contains(event.target)) closeAccountMenu();
        });

        document.addEventListener("keydown", event => {
            if (event.key === "Escape" && !accountMenuPanel.hidden) {
                closeAccountMenu(true);
            }
        });
    }

    const laundryRoomFloor = document.querySelector("[data-laundry-room-floor]");
    const laundryRoomNumber = document.querySelector("[data-laundry-room-number]");
    if (laundryRoomFloor && laundryRoomNumber) {
        const syncRoomOptions = () => {
            const floor = laundryRoomFloor.value;
            const availableOptions = [...laundryRoomNumber.options]
                .filter(option => option.dataset.floor === floor);
            laundryRoomNumber.querySelectorAll("option").forEach(option => {
                option.hidden = option.dataset.floor !== floor;
                option.disabled = option.hidden;
            });
            if (!availableOptions.some(option => option.value === laundryRoomNumber.value)) {
                laundryRoomNumber.value = availableOptions[0]?.value || "";
            }
        };

        laundryRoomFloor.addEventListener("change", syncRoomOptions);
        laundryRoomNumber.addEventListener("change", () => {
            const selectedOption = laundryRoomNumber.selectedOptions[0];
            if (selectedOption?.dataset.floor) {
                laundryRoomFloor.value = selectedOption.dataset.floor;
                syncRoomOptions();
            }
        });
        syncRoomOptions();
    }

    // Names and surnames: allow letters only. Server-side validation remains authoritative.
    document.querySelectorAll("[data-letters-only]").forEach((input) => {
        input.addEventListener("input", () => {
            input.value = Array.from(input.value)
                .filter((char) => /\p{L}/u.test(char))
                .slice(0, 30)
                .join("");
        });
    });

    // Room number: integer from 1 to 1000.
    document.querySelectorAll('input[name="roomNumber"]').forEach((input) => {
        input.addEventListener("input", () => {
            input.value = input.value.replace(/\D/g, "").slice(0, 4);
            if (input.value !== "") {
                const value = Number(input.value);
                if (value > 1000) input.value = "1000";
                if (value < 1) input.value = "";
            }
        });
    });

    document.querySelectorAll("[data-password-toggle]").forEach((button) => {
        button.addEventListener("click", () => {
            const input = document.getElementById(button.dataset.passwordToggle);
            if (!input) return;

            const showing = input.type === "password";
            input.type = showing ? "text" : "password";

            const icon = button.querySelector(".eye-icon");
            if (icon) icon.textContent = "👁";

            button.setAttribute("aria-label", showing ? "Скрыть пароль" : "Показать пароль");
            button.setAttribute("title", showing ? "Скрыть пароль" : "Показать пароль");
            button.setAttribute("aria-pressed", showing ? "true" : "false");
            button.classList.toggle("is-visible", showing);
        });
    });

    // Restore the exact scroll position after a page refresh.
    // A separate key is used for every page, so the schedule and admin panel
    // do not overwrite each other's position.
    const scrollKey = "laundry-scroll:" + window.location.pathname + window.location.search;
    const savedScroll = sessionStorage.getItem(scrollKey);
    if (savedScroll !== null) {
        const y = Number(savedScroll);
        if (Number.isFinite(y)) {
            requestAnimationFrame(() => {
                requestAnimationFrame(() => window.scrollTo(0, y));
            });
        }
    }

    let saveScrollTimer = null;
    const saveScroll = () => {
        clearTimeout(saveScrollTimer);
        saveScrollTimer = setTimeout(() => {
            sessionStorage.setItem(scrollKey, String(Math.max(0, Math.round(window.scrollY))));
        }, 50);
    };
    window.addEventListener("scroll", saveScroll, { passive: true });
    window.addEventListener("beforeunload", () => {
        sessionStorage.setItem(scrollKey, String(Math.max(0, Math.round(window.scrollY))));
    });

    // Admin panel tabs.
    function initAdminTabs() {
        const adminTabs = document.querySelectorAll("[data-admin-tab]");
        const adminPanels = document.querySelectorAll("[data-admin-panel]");
        if (!adminTabs.length) return;

        const isWardenShell = document.querySelector(".warden-shell") !== null;
        const adminStorageKey = isWardenShell
            ? "laundry-warden-active-tab"
            : "laundry-admin-active-tab";
        const serverTab = isWardenShell
            ? document.body.dataset.wardenActiveTab || ""
            : document.body.dataset.adminActiveTab || "";
        const queryTab = new URLSearchParams(window.location.search).get("tab") || "";
        const hashTab = window.location.hash.replace("#", "");
        const savedTab = localStorage.getItem(adminStorageKey) || "";
        const isMobileAdmin = window.matchMedia("(max-width: 1000px)").matches;
        const explicitTab = [queryTab, hashTab]
            .map(x => x.trim().toLowerCase())
            .find(x => [...adminTabs].some(t => t.dataset.adminTab === x));
        const requested = explicitTab || (
            isMobileAdmin ? "" : savedTab.trim().toLowerCase() || serverTab.trim().toLowerCase() || "accounts"
        );
        const adminShell = document.querySelector(".admin-shell");

        adminPanels.forEach((panel) => {
            if (panel.querySelector(".admin-mobile-back")) return;
            const backButton = document.createElement("button");
            backButton.type = "button";
            backButton.className = "admin-mobile-back";
            backButton.textContent = "← К разделам";
            backButton.addEventListener("click", () => {
                adminShell?.classList.remove("admin-section-open");
                adminTabs.forEach(tab => tab.classList.remove("active"));
                adminPanels.forEach(item => item.classList.remove("active"));
                localStorage.removeItem(adminStorageKey);
                if (history.replaceState) {
                    const url = new URL(window.location.href);
                    url.searchParams.delete("tab");
                    history.replaceState(null, "", url.pathname + url.search + url.hash);
                }
            });
            panel.prepend(backButton);
        });

        function activateAdminTab(target, updateUrl = true, openMobilePanel = false) {
            const tab = [...adminTabs].find(t => t.dataset.adminTab === target);
            if (!tab) return;
            adminTabs.forEach((t) => t.classList.toggle("active", t === tab));
            adminPanels.forEach((panel) => panel.classList.toggle("active", panel.dataset.adminPanel === target));
            localStorage.setItem(adminStorageKey, target);
            if (adminShell) adminShell.dataset.activeAdminTab = target;
            document.body.dataset.adminActiveTab = target;
            if (adminShell && window.matchMedia("(max-width: 1000px)").matches) {
                adminShell.classList.toggle("admin-section-open", openMobilePanel);
            }
            if (updateUrl && history.replaceState) {
                const url = new URL(window.location.href);
                url.searchParams.set("tab", target);
                url.hash = "";
                history.replaceState(null, "", url.pathname + url.search + url.hash);
            }
        }

        adminTabs.forEach((tab) => {
            if (tab.dataset.bound === "1") return;
            tab.dataset.bound = "1";
            tab.addEventListener("click", () => {
                const openMobilePanel = window.matchMedia("(max-width: 1000px)").matches;
                activateAdminTab(tab.dataset.adminTab, true, openMobilePanel);
            });
        });
        if (requested) {
            activateAdminTab(requested, false, isMobileAdmin && Boolean(explicitTab));
        } else {
            adminTabs.forEach(tab => tab.classList.remove("active"));
            adminPanels.forEach(panel => panel.classList.remove("active"));
            adminShell?.classList.remove("admin-section-open");
        }
    }
    initAdminTabs();

    // Server-side chat notification badges.
    (function () {
        const userBadge = document.getElementById("user-chat-badge");
        const adminBadge = document.getElementById("admin-chat-badge");
        if (!userBadge && !adminBadge) return;

        async function updateBadge(url, badge) {
            if (!badge) return;
            try {
                const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
                if (!response.ok) return;
                const data = await response.json();
                const count = Number(data.count || 0);
                badge.textContent = count > 99 ? "99+" : String(count);
                badge.hidden = count <= 0;
            } catch (_) { }
        }

        function refresh() {
            if (userBadge) updateBadge("/Chat/UnreadCount", userBadge);
            if (adminBadge) updateBadge("/Admin/UnreadChatCount", adminBadge);
        }

        refresh();
        window.setInterval(refresh, 10000);
    })();
    // ===== Sliding side chat panel =====
    // The drawer is a single global component. Event delegation is intentional:
    // the admin live-refresh replaces its buttons, so direct listeners would be lost.
    const drawer = document.getElementById("side-drawer");
    const drawerContent = document.getElementById("side-drawer-content");
    const drawerTitle = document.getElementById("side-drawer-title");
    const drawerClose = document.getElementById("side-drawer-close");
    const drawerBackdrop = document.getElementById("side-drawer-backdrop");
    let drawerType = null;
    let drawerKey = null;

    document.addEventListener("click", async event => {
        const button = event.target.closest?.("[data-copy-account-id]");
        if (!button) return;

        const accountId = button.dataset.copyAccountId || "";
        const status = button.closest(".account-id-card")?.querySelector(".account-id-copy-status");
        let copied = false;

        try {
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(accountId);
                copied = true;
            }
        } catch (_) {
            copied = false;
        }

        if (!copied) {
            const temporaryInput = document.createElement("textarea");
            temporaryInput.value = accountId;
            temporaryInput.setAttribute("readonly", "");
            temporaryInput.style.position = "fixed";
            temporaryInput.style.opacity = "0";
            document.body.appendChild(temporaryInput);
            temporaryInput.select();
            try {
                copied = document.execCommand("copy");
            } catch (_) {
                copied = false;
            } finally {
                temporaryInput.remove();
            }
        }

        if (status) status.textContent = copied ? "ID скопирован." : "Не удалось скопировать. Выделите ID вручную.";
        if (copied) {
            button.textContent = "Скопировано";
            window.setTimeout(() => { button.textContent = "Скопировать"; }, 1800);
        }
    });

    function closeSidePanel() {
        if (!drawer) return;
        drawer.classList.remove("open");
        document.body.classList.remove("side-panel-open", "side-panel-admin", "side-panel-user");
        drawer.setAttribute("aria-hidden", "true");
        if (drawerBackdrop) drawerBackdrop.hidden = true;
        drawerType = null;
        drawerKey = null;
    }

    function getChatKey(trigger) {
        const input = trigger?.closest("form")?.querySelector("[data-chat-key-input], input[name='key']");
        const value = input?.value?.trim();
        if (value) return value.toUpperCase();
        const urlKey = new URLSearchParams(window.location.search).get("key");
        if (urlKey) return urlKey.trim().toUpperCase();
        return document.body.dataset.chatKey || "";
    }

    function setDrawerOpen(type) {
        drawerType = type;
        document.body.classList.add("side-panel-open");
        document.body.classList.toggle("side-panel-admin", type === "admin-chat");
        document.body.classList.toggle("side-panel-user", type === "user-chat");
        drawer?.classList.add("open");
        drawer?.setAttribute("aria-hidden", "false");
        if (drawerBackdrop) drawerBackdrop.hidden = false;
    }

    async function loadSidePanel(type, key = "") {
        if (!drawer || !drawerContent) return;
        const isAdmin = type === "admin-chat";
        drawerKey = key || null;
        drawerTitle.textContent = isAdmin ? "Связь с пользователями" : "Связь с админом";
        drawerContent.innerHTML = '<div class="side-drawer-loading">Загрузка чата…</div>';
        setDrawerOpen(type);

        const url = isAdmin
            ? "/Admin?tab=chat"
            : "/Chat/Index" + (key ? "?key=" + encodeURIComponent(key) : "");

        try {
            const response = await fetch(url, {
                credentials: "same-origin",
                cache: "no-store",
                headers: { "X-Requested-With": "XMLHttpRequest" }
            });
            if (!response.ok) throw new Error("HTTP " + response.status);

            const html = await response.text();
            const doc = new DOMParser().parseFromString(html, "text/html");

            let content;
            if (isAdmin) {
                content = doc.querySelector('[data-admin-panel="chat"]');
            } else {
                content = doc.querySelector("[data-side-chat-content]");
            }
            if (!content) throw new Error("chat content not found");

            // Never copy the hidden admin tab state into the drawer.
            content.classList.add("drawer-chat-content");
            content.style.display = "block";
            drawerContent.innerHTML = content.outerHTML;
            bindDrawerForms();

            const messages = drawerContent.querySelector(".chat-messages");
            if (messages) messages.scrollTop = messages.scrollHeight;

            // If the key was invalid, leave the key form visible inside the drawer.
            const error = drawerContent.querySelector(".notice.error");
            const keyInput = drawerContent.querySelector("input[name='key']");
            if (error && keyInput) keyInput.focus();
        } catch (_) {
            drawerContent.innerHTML = '<div class="notice error">Не удалось загрузить чат. Проверьте ключ и попробуйте ещё раз.</div>';
        }
    }

    function bindDrawerForms() {
        if (!drawerContent) return;

        drawerContent.querySelectorAll("form").forEach(form => {
            if (form.dataset.drawerBound === "1") return;
            form.dataset.drawerBound = "1";

            // GET key form: validate/load inside the drawer, never navigate to a new page.
            if ((form.method || "get").toLowerCase() === "get" &&
                (form.action || "").includes("/Chat/Index")) {
                form.addEventListener("submit", async event => {
                    event.preventDefault();
                    const key = form.querySelector("input[name='key']")?.value?.trim().toUpperCase() || "";
                    if (!key) return;
                    await loadSidePanel("user-chat", key);
                });
                return;
            }

            form.addEventListener("submit", async event => {
                event.preventDefault();
                const button = form.querySelector("button[type=submit]");
                if (button) button.disabled = true;

                try {
                    const response = await fetch(form.action || window.location.href, {
                        method: form.method || "POST",
                        body: new FormData(form),
                        credentials: "same-origin",
                        cache: "no-store",
                        headers: { "X-Requested-With": "XMLHttpRequest" }
                    });
                    if (!response.ok) throw new Error("HTTP " + response.status);

                    const html = await response.text();
                    const doc = new DOMParser().parseFromString(html, "text/html");
                    const isAdmin = drawerType === "admin-chat";
                    const content = isAdmin
                        ? doc.querySelector('[data-admin-panel="chat"]')
                        : doc.querySelector("[data-side-chat-content]");

                    if (content) {
                        content.classList.add("drawer-chat-content");
                        content.style.display = "block";
                        drawerContent.innerHTML = content.outerHTML;
                        bindDrawerForms();
                        const messages = drawerContent.querySelector(".chat-messages");
                        if (messages) messages.scrollTop = messages.scrollHeight;
                    }
                } catch (_) {
                    // Keep the message form intact if the request failed.
                } finally {
                    if (button) button.disabled = false;
                }
            });
        });
    }

    // Stable event delegation survives the admin panel's silent DOM replacements.
    document.addEventListener("click", event => {
        const trigger = event.target.closest?.("[data-side-panel]");
        if (!trigger) return;

        event.preventDefault();
        event.stopPropagation();

        const type = trigger.dataset.sidePanel;
        if (type === "announcements-board") return;
        if (type === "admin-chat") {
            loadSidePanel("admin-chat");
            return;
        }

        const key = getChatKey(trigger);
        loadSidePanel("user-chat", key);
    });

    // If the page was opened directly with the chat tab requested, open the
    // chat in the same right-side drawer instead of rendering it in the page.
    const autoOpenChat = document.querySelector("[data-auto-open-chat='1']");
    if (autoOpenChat) {
        window.setTimeout(() => autoOpenChat.click(), 80);
    }

    // A direct /Chat/Index page should also use the drawer layout.
    // The normal navigation link already opens the drawer; this only handles
    // an old bookmark/direct URL.
    if (document.querySelector("#site-main > .chat-page") &&
        !document.querySelector("[data-side-panel='user-chat'][data-auto-open-chat='1']")) {
        const directChat = document.querySelector("#site-main > .chat-page");
        if (directChat && drawer) {
            loadSidePanel("user-chat", getChatKey(null));
        }
    }

    drawerClose?.addEventListener("click", closeSidePanel);
    drawerBackdrop?.addEventListener("click", closeSidePanel);
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") closeSidePanel();
    });

    // ===== Quiet live refresh =====
    function hasActiveEditor() {
        const active = document.activeElement;
        return !!active && ["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(active.tagName);
    }

    document.addEventListener("input", markFormChanges);
    document.addEventListener("change", markFormChanges);
    document.addEventListener("submit", (event) => {
        if (event.target instanceof HTMLFormElement) {
            event.target.dataset.dirty = "false";
        }
    });

    function markFormChanges(event) {
        const form = event.target?.form;
        if (form instanceof HTMLFormElement) {
            form.dataset.dirty = "true";
        }
    }

    let adminRefreshInProgress = false;
    function comparablePanelContent(panel) {
        const clone = panel.cloneNode(true);
        clone.querySelectorAll(".admin-mobile-back").forEach(button => button.remove());
        return clone.innerHTML;
    }

    async function refreshAdminSilently() {
        if (document.hidden || adminRefreshInProgress || document.querySelector(".warden-shell")) return;
        const shell = document.querySelector(".admin-shell");
        const scheduleForm = shell?.querySelector("#scheduleChangesForm");
        if (!shell || hasActiveEditor() || scheduleForm?.dataset.dirty === "true") return;
        const activeTab = document.querySelector("[data-admin-tab].active")?.dataset.adminTab || "accounts";
        const activePanel = shell.querySelector(`[data-admin-panel="${activeTab}"]`);
        if (!activePanel || activePanel.querySelector('form[data-dirty="true"]')) return;
        adminRefreshInProgress = true;
        try {
            const refreshUrl = new URL("/Admin", window.location.origin);
            refreshUrl.search = window.location.search;
            refreshUrl.searchParams.set("tab", activeTab);
            const response = await fetch(refreshUrl, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) return;
            const html = await response.text();
            const doc = new DOMParser().parseFromString(html, "text/html");
            const fresh = doc.querySelector(".admin-shell");
            const freshPanel = fresh?.querySelector(`[data-admin-panel="${activeTab}"]`);
            if (!fresh || !freshPanel) return;

            const currentTabButtons = shell.querySelectorAll("[data-admin-tab]");
            currentTabButtons.forEach(button => {
                const freshButton = [...fresh.querySelectorAll("[data-admin-tab]")]
                    .find(candidate => candidate.dataset.adminTab === button.dataset.adminTab);
                const currentBadge = button.querySelector(".tab-badge");
                const freshBadge = freshButton?.querySelector(".tab-badge");
                if (currentBadge && freshBadge &&
                    (currentBadge.textContent !== freshBadge.textContent || currentBadge.hidden !== freshBadge.hidden)) {
                    currentBadge.textContent = freshBadge.textContent;
                    currentBadge.hidden = freshBadge.hidden;
                }
            });

            if (comparablePanelContent(freshPanel) !== comparablePanelContent(activePanel)) {
                freshPanel.classList.toggle("active", activePanel.classList.contains("active"));
                freshPanel.classList.add("live-refresh-in");
                activePanel.replaceWith(freshPanel);
                requestAnimationFrame(() => freshPanel.classList.remove("live-refresh-in"));
                initAdminTabs();
            }
        } catch (_) {
        } finally {
            adminRefreshInProgress = false;
        }
    }

    async function refreshScheduleSilently() {
        if (document.hidden || !document.querySelector(".schedule") || hasActiveEditor()) return;
        try {
            const response = await fetch(window.location.pathname + window.location.search, { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) return;
            const html = await response.text();
            const doc = new DOMParser().parseFromString(html, "text/html");
            const current = document.querySelector(".schedule");
            const fresh = doc.querySelector(".schedule");
            if (current && fresh && current.innerHTML !== fresh.innerHTML) {
                fresh.classList.add("live-refresh-in");
                current.replaceWith(fresh);
                requestAnimationFrame(() => fresh.classList.remove("live-refresh-in"));
            }
        } catch (_) { }
    }

    if (document.querySelector(".admin-shell:not(.warden-shell)")) window.setInterval(refreshAdminSilently, 10000);
    if (document.querySelector(".schedule")) window.setInterval(refreshScheduleSilently, 10000);

});

// Announcement side panel
document.addEventListener('click', function(e){
 const link=e.target.closest('[data-side-panel="announcements-board"]');
 if(!link) return;
 e.preventDefault();
 const drawer=document.getElementById('side-drawer');
 const content=document.getElementById('side-drawer-content');
 const board=document.getElementById('announcements-board');
 if(drawer&&content&&board){
  document.getElementById('side-drawer-title').textContent='📢 Объявления';
  content.innerHTML=board.innerHTML;
  drawer.classList.add('open');
 }
});

// Preview attachment selection in admin announcements
document.addEventListener("DOMContentLoaded", function(){
 const file=document.getElementById("announcementFile");
 const status=document.getElementById("fileStatus");
 if(file && status){
   file.addEventListener("change", function(){
     if(file.files.length){
       status.hidden=false;
       status.textContent="📎 Добавлено: " + file.files[0].name;
     } else {
       status.hidden=true;
       status.textContent="";
     }
   });
 }
});
