const STATUSES = ["todo", "ready", "in_progress", "needs_review", "done", "failed"];
const COLUMN_LABEL = { todo: "To Do", ready: "Ready", in_progress: "In Progress", needs_review: "Needs Review", done: "Done", failed: "Failed" };

function css(varName) {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

async function fetchJSON(url, opts) {
  const base = window.TICKETBOARD_API_BASE || "";
  const resp = await fetch(base + url, opts);
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`${resp.status}: ${body}`);
  }
  return resp.json();
}

const { createApp } = Vue;

createApp({
  data() {
    return {
      STATUSES,
      COLUMN_LABEL,
      projects: [],
      tickets: [],
      workerStatus: null,
      dispatcherLog: [],
      logError: false,

      detailOpen: false,
      currentTicketId: null,
      currentTicket: null,
      chatInput: "",
      chatSending: false,

      newTicketOpen: false,
      selectedProjectId: null,
      newProject: { name: "", repoPath: "", testCommand: "" },
      newTicket: { title: "", objective: "", acceptance: "", filesHint: "" },
      attachmentFile: null,
      submitting: false,
      browsing: false,

      _boardTimer: null,
      _workerTimer: null,
      _logTimer: null,
    };
  },

  computed: {
    COLUMN_VAR() {
      return {
        todo: css("--todo"), ready: css("--ready"), in_progress: css("--building"),
        needs_review: css("--needs-review"), done: css("--done"), failed: css("--failed"),
      };
    },
    COLUMN_TINT_VAR() {
      return {
        todo: css("--todo-tint"), ready: css("--ready-tint"), in_progress: css("--building-tint"),
        needs_review: css("--needs-review-tint"), done: css("--done-tint"), failed: css("--failed-tint"),
      };
    },
    projectsById() {
      const m = {};
      for (const p of this.projects) m[p.id] = p;
      return m;
    },
    byStatus() {
      const m = {};
      for (const s of STATUSES) m[s] = [];
      for (const t of this.tickets) (m[t.status] || []).push(t);
      return m;
    },
    totalOpen() {
      return this.tickets.filter((t) => t.status !== "done").length;
    },
    nowBuilding() {
      return this.byStatus.in_progress[0] || null;
    },
    hbClass() {
      if (!this.workerStatus) return "hb-unknown";
      if (this.workerStatus.last_heartbeat === null) return "hb-stale";
      return this.workerStatus.stale ? "hb-stale" : "hb-fresh";
    },
    hbTitle() {
      if (!this.workerStatus) return "Unknown";
      if (this.workerStatus.last_heartbeat === null) return "Never seen";
      if (this.workerStatus.stale) return "Dispatcher stale";
      return this.workerStatus.locked_ticket_id
        ? `Building ${this.ticketCode({ id: this.workerStatus.locked_ticket_id })}`
        : "Dispatcher alive";
    },
    hbSub() {
      if (!this.workerStatus) return "could not reach worker status";
      if (this.workerStatus.last_heartbeat === null) return "no worker has reported in yet";
      if (this.workerStatus.stale) {
        const mins = Math.round(this.workerStatus.seconds_since_heartbeat / 60);
        return `last seen ${mins}m ago`;
      }
      const secs = Math.round(this.workerStatus.seconds_since_heartbeat);
      return `heartbeat ${secs}s ago`;
    },
    detailHue() {
      return this.currentTicket ? (this.COLUMN_VAR[this.currentTicket.status] || this.COLUMN_VAR.todo) : "";
    },
    detailTint() {
      return this.currentTicket ? (this.COLUMN_TINT_VAR[this.currentTicket.status] || this.COLUMN_TINT_VAR.todo) : "";
    },
    approveConfig() {
      if (!this.currentTicket) return null;
      const s = this.currentTicket.status;
      if (s === "todo") {
        return { title: "Ready to approve?", sub: "moves to Ready — the dispatcher builds it instantly", btn: "Approve & promote" };
      }
      if (s === "needs_review") {
        return { title: "Judge flagged this build", sub: "re-promote to Ready to rebuild, e.g. after refining the criteria via chat", btn: "Re-promote to Ready" };
      }
      if (s === "failed") {
        return { title: "Build failed", sub: "re-promote to Ready to retry", btn: "Re-promote to Ready" };
      }
      return null;
    },
  },

  methods: {
    ticketCode(t) {
      return `TB-${t.id}`;
    },
    timeOnly(ts) {
      if (!ts) return "";
      const parts = ts.split(" ");
      return parts.length > 1 ? parts[1].slice(0, 8) : ts;
    },
    projectName(projectId) {
      const p = this.projectsById[projectId];
      return p ? p.name : "project #" + projectId;
    },
    eventColor(eventType) {
      if (eventType === "fail") return css("--failed");
      if (eventType === "finish") return css("--done");
      if (eventType === "start") return css("--building");
      return "oklch(0.82 0.005 60)";
    },
    logColor(eventType) {
      if (eventType === "fail") return css("--failed");
      if (eventType === "finish") return css("--done");
      if (eventType === "start") return css("--building");
      return css("--muted");
    },
    openCountForProject(projectId) {
      return this.tickets.filter((t) => t.project_id === projectId && t.status !== "done").length;
    },

    async loadProjects() {
      this.projects = await fetchJSON("/projects");
    },
    async loadBoard() {
      await this.loadProjects();
      this.tickets = await fetchJSON("/tickets");
    },
    async loadWorkerStatus() {
      try {
        this.workerStatus = await fetchJSON("/worker/status");
      } catch (err) {
        this.workerStatus = null;
      }
    },
    async loadDispatcherLog() {
      try {
        this.dispatcherLog = await fetchJSON("/events/recent?limit=20");
        this.logError = false;
      } catch (err) {
        this.logError = true;
      }
    },
    refreshAll() {
      this.loadBoard();
      this.loadWorkerStatus();
      this.loadDispatcherLog();
    },

    async promoteTicket(ticketId) {
      await fetchJSON(`/tickets/${ticketId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "ready" }),
      });
      await this.loadBoard();
    },

    async openDetail(ticketId) {
      this.currentTicketId = ticketId;
      this.currentTicket = await fetchJSON(`/tickets/${ticketId}`);
      this.detailOpen = true;
    },
    closeDetail() {
      this.detailOpen = false;
      this.currentTicketId = null;
      this.currentTicket = null;
      this.chatInput = "";
    },
    async approveFromDetail() {
      if (!this.currentTicketId) return;
      await this.promoteTicket(this.currentTicketId);
      this.closeDetail();
    },

    onChatKeydown(e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !e.shiftKey)) {
        e.preventDefault();
        this.sendChatMessage();
      }
    },
    async sendChatMessage() {
      const text = this.chatInput.trim();
      if (!text || !this.currentTicketId) return;
      this.chatSending = true;
      this.chatInput = "";
      try {
        await fetchJSON(`/tickets/${this.currentTicketId}/messages`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }),
        });
        this.currentTicket = await fetchJSON(`/tickets/${this.currentTicketId}`);
      } catch (err) {
        alert(`Chat failed: ${err.message}`);
      } finally {
        this.chatSending = false;
      }
    },

    openNewTicketModal() {
      this.selectedProjectId = null;
      this.newProject = { name: "", repoPath: "", testCommand: "" };
      this.newTicket = { title: "", objective: "", acceptance: "", filesHint: "" };
      this.attachmentFile = null;
      this.loadProjects();
      this.newTicketOpen = true;
    },
    closeNewTicketModal() {
      this.newTicketOpen = false;
    },
    onFileChange(e) {
      this.attachmentFile = e.target.files[0] || null;
    },
    async browseFolder() {
      this.browsing = true;
      try {
        const result = await fetchJSON("/browse-folder", { method: "POST" });
        if (result.path) this.newProject.repoPath = result.path;
      } catch (err) {
        alert(`Could not open folder picker: ${err.message}`);
      } finally {
        this.browsing = false;
      }
    },
    async resolveProjectId() {
      if (this.selectedProjectId) return this.selectedProjectId;
      const name = this.newProject.name.trim();
      const repoPath = this.newProject.repoPath.trim();
      const testCommand = this.newProject.testCommand.trim();
      if (!name || !repoPath) {
        throw new Error("Select an existing project or fill in name + repo path for a new one.");
      }
      const project = await fetchJSON("/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, repo_path: repoPath, test_command: testCommand || null }),
      });
      return project.id;
    },
    async submitNewTicket() {
      this.submitting = true;
      try {
        const projectId = await this.resolveProjectId();
        const { title, objective, acceptance, filesHint } = this.newTicket;
        if (!title || !objective || !acceptance) {
          throw new Error("Title, objective, and acceptance criteria are required.");
        }
        const ticket = await fetchJSON(`/projects/${projectId}/ticket`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title, objective, acceptance_criteria: acceptance, files_hint: filesHint || null,
          }),
        });
        if (this.attachmentFile) {
          const formData = new FormData();
          formData.append("file", this.attachmentFile);
          await fetchJSON(`/tickets/${ticket.id}/attachments`, { method: "POST", body: formData });
        }
        this.closeNewTicketModal();
        await this.loadBoard();
      } catch (err) {
        alert(`Could not create ticket: ${err.message}`);
      } finally {
        this.submitting = false;
      }
    },
  },

  mounted() {
    this.refreshAll();
    this._boardTimer = setInterval(() => this.loadBoard(), 10000);
    this._workerTimer = setInterval(() => this.loadWorkerStatus(), 15000);
    this._logTimer = setInterval(() => this.loadDispatcherLog(), 15000);

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        this.closeDetail();
        this.closeNewTicketModal();
      }
    });
  },
}).mount("#app");
