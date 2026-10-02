document.addEventListener("DOMContentLoaded", () => {
    // Names and surnames: allow letters only. Server-side validation remains authoritative.
    document.querySelectorAll("[data-letters-only]").forEach((input) => {
        input.addEventListener("input", () => {
            input.value = Array.from(input.value)
                .filter((char) => /\p{L}/u.test(char))
                .slice(0, 30)
                .join("");
        
    const announcementFile = document.getElementById("announcementFile");
    const fileStatus = document.getElementById("fileStatus");
    if (announcementFile && fileStatus) {
        announcementFile.addEventListener("change", () => {
            if (announcementFile.files.length) {
                fileStatus.style.display = "block";
                fileStatus.textContent = "📎 Добавлено: " + announcementFile.files[0].name;
            } else {
                fileStatus.style.display = "none";
            }
        });
    }
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

        const adminStorageKey = "laundry-admin-active-tab";
        const serverTab = document.body.dataset.adminActiveTab || "";
        const queryTab = new URLSearchParams(window.location.search).get("tab") || "";
        const hashTab = window.location.hash.replace("#", "");
        const savedTab = localStorage.getItem(adminStorageKey) || "";
        const requested = [serverTab, queryTab, hashTab, savedTab]
            .map(x => x.trim().toLowerCase())
            .find(x => [...adminTabs].some(t => t.dataset.adminTab === x)) || "accounts";

        function activateAdminTab(target, updateUrl = true) {
            const tab = [...adminTabs].find(t => t.dataset.adminTab === target);
            if (!tab) return;
            adminTabs.forEach((t) => t.classList.toggle("active", t === tab));
            adminPanels.forEach((panel) => panel.classList.toggle("active", panel.dataset.adminPanel === target));
            localStorage.setItem(adminStorageKey, target);
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
            tab.addEventListener("click", () => activateAdminTab(tab.dataset.adminTab));
        });
        activateAdminTab(requested, false);
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

    async function refreshAdminSilently() {
        const shell = document.querySelector(".admin-shell");
        if (!shell || hasActiveEditor()) return;
        const activePanel = document.querySelector("[data-admin-panel].active");
        const activeTab = document.querySelector("[data-admin-tab].active")?.dataset.adminTab || "accounts";
        try {
            const response = await fetch("/Admin?tab=" + encodeURIComponent(activeTab), { credentials: "same-origin", cache: "no-store" });
            if (!response.ok) return;
            const html = await response.text();
            const doc = new DOMParser().parseFromString(html, "text/html");
            const fresh = doc.querySelector(".admin-shell");
            if (!fresh) return;
            if (fresh.innerHTML !== shell.innerHTML) {
                const scrollY = window.scrollY;
                fresh.classList.add("live-refresh-in");
                shell.replaceWith(fresh);
                requestAnimationFrame(() => fresh.classList.remove("live-refresh-in"));
                initAdminTabs();
                window.scrollTo(0, scrollY);
                if (document.querySelector("[data-side-panel=admin-chat]")) {
                    document.querySelector("[data-side-panel=admin-chat]").addEventListener("click", event => { event.preventDefault(); loadSidePanel("admin-chat"); });
                }
            }
        } catch (_) { }
    }

    async function refreshScheduleSilently() {
        if (!document.querySelector(".schedule") || hasActiveEditor()) return;
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

    if (document.querySelector(".admin-shell")) window.setInterval(refreshAdminSilently, 5000);
    if (document.querySelector(".schedule")) window.setInterval(refreshScheduleSilently, 5000);

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
       status.style.display="block";
       status.textContent="📎 Добавлено: " + file.files[0].name;
     } else {
       status.style.display="none";
     }
   });
 }
});
