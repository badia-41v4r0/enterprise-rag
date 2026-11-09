// Interface texts of the setup wizard, the setup assistant, the checklist and the password change, kept in one
// place so they can be translated later (PLAN-v0.4: English only for now). Older screens still keep their texts
// inline; moving them here is a follow-up.

export const T = {
  product: "EnterpriseRAG",
  demoBanner: "Demo installation, not for real data. The demo accounts share a published password: keep this instance on localhost or a private network.",
  wizardTitle: "Set up EnterpriseRAG",
  wizardIntro:
    "This wizard takes a fresh installation to a working, secure system in about half an hour. Progress is saved on the server: you can close the tab and come back.",
  token: {
    title: "Enter the setup token",
    why: "Only the person who installed the software can run the setup. The token was printed by `erag init`, or written to the application log and to a file in the data volume at the first start.",
    fileHint: (file: string) => `If you did not run erag init: docker compose logs app | grep "Setup token", or read ${file} inside the app container.`,
    initHint: "Use the token printed by `erag init`.",
    lost: "Lost it? Run `erag setup --new-token` on the server (docker compose exec app erag setup --new-token).",
    label: "Setup token",
    submit: "Continue",
    network: "Until the setup is finished, keep this port on localhost or a private network.",
  },
  steps: [
    { key: "license", title: "Welcome and license", why: "EnterpriseRAG is source-available, not open source. Read how you may use it; your acceptance is recorded on this installation and nothing is sent anywhere." },
    { key: "environment", title: "Environment check", why: "These are the things that commonly break: database, master key, secrets, HTTPS, storage, worker and Ollama. The wizard only checks them; fix red items in the environment file or on the host and press Check again." },
    { key: "admin", title: "Administrator", why: "The administrator manages users, access and models. Choose a strong password and, ideally, protect the account with an authenticator app right away." },
    { key: "install_type", title: "Installation type", why: "Start empty and build your own structure, or explore a fictional company with mock data first. The demo can be removed later from the admin area." },
    { key: "organization", title: "Organization", why: "Your company name appears in the header. The classification policy decides what may leave the building: confidential data never goes to a cloud model." },
    { key: "models", title: "Models", why: "Answers come from a chat model and search uses an embedding model, both in your local Ollama by default. A cloud provider is optional and only receives what the classification policy allows." },
    { key: "access", title: "Access structure", why: "Containers hold documents and carry a classification; roles group people; grants give a role access to a container. Nothing is created for you: build the structure your company needs." },
    { key: "people", title: "People", why: "Create the accounts of the people who will use the system. Their passwords are temporary: each person chooses their own at the first login." },
    { key: "knowledge", title: "Knowledge", why: "Upload the first documents into a container, or connect a folder or a web site. Documents are split and indexed by the worker; the progress is shown live." },
    { key: "check", title: "Does it work?", why: "Ask a question as a chosen user and see what that person may read, which sources were used, which model answered and the classification, before anyone else logs in." },
    { key: "tools", title: "Tools and router (optional)", why: "MCP tools let the model act (search tickets, read files) and the model router picks a model per question. Both start off or empty; set them up later if you need them." },
    { key: "finish", title: "Review and finish", why: "A summary of what was set up and a security checklist. Finishing closes the setup mode, destroys the setup token and signs you in." },
  ],
  license: {
    checkbox: "I have read this notice and accept the license terms for this installation",
    name: "Your name (recorded with the acceptance)",
    note: "This is a record and a deliberate pause, not a technical lock: there is no license key and no network call.",
  },
  env: {
    recheck: "Check again",
    allGood: "Everything required is in place.",
    hasFailures: "Some checks failed. You may continue, but fix them before people use the system.",
  },
  admin: {
    created: "Administrator created and signed in.",
    signInInstead: "An administrator already exists for this setup: sign in to continue.",
    strength: ["too short", "weak", "fair", "good", "strong"],
    mfaOffer: "Protect this account with an authenticator app now (recommended).",
    mfaStart: "Enable MFA",
    mfaDone: "MFA is on. Store these recovery codes somewhere safe; they are shown only once:",
  },
  installType: {
    empty: "Empty installation (default)",
    emptyWhy: "Nothing is created. You build containers, roles, grants and users yourself in the next steps.",
    demo: "Demo with mock data",
    demoWhy: "Installs the fictional ACME Corp: its people, roles, containers, documents and tools. The demo accounts share a published password, so keep the instance on a private network. Your administrator account stays yours (no demo administrator is created).",
    installing: "Installing the demo... this takes a few seconds; documents are indexed by the worker afterwards.",
  },
  org: {
    company: "Company name",
    policyTitle: "What may leave the building?",
    public: "Only public data may go to approved cloud providers (strictest; recommended default)",
    internal: "Public and internal data may go to approved cloud providers",
    never: "Confidential data never leaves: it is only answered by local models.",
  },
  models: {
    detect: "Local Ollama",
    unreachable: "Ollama is not reachable. Start it on the host and check ERAG_OLLAMA_BASE_URL, then reload.",
    embedding: "Embedding model",
    locked: "Locked: documents are already indexed with it. Changing it means re-indexing everything.",
    embeddingFixed: "Set by ERAG_EMBED_MODEL (1024-dimensional vectors). It can only be changed before the first document is indexed, in the environment file.",
    chat: "Chat model",
    makeDefault: "Use as chat model",
    test: "Test",
    download: "Download",
    downloadTitle: "Download a model from the Ollama registry",
    confirmTitle: "Confirm the download",
    confirmText: (name: string, size: string, source: string) => `Download ${name} (${size}) from ${source} into the local Ollama? Nothing is downloaded until you confirm.`,
    cancel: "Cancel download",
    cloudTitle: "Optional: a cloud provider",
    cloudWhy: "Its API key goes straight to the credential store; it starts cleared for public data only and has a monthly budget. Skip this to stay fully local.",
  },
  access: {
    missing: "Still missing:",
    containerHelp: "A container is a folder of documents with one classification: public (anyone may see it, may go to the cloud), internal (employees; may go to approved cloud providers if the policy allows) or confidential (local models only).",
    roleHelp: "A role groups people with the same access, for example HR or Engineering. Grants give a role read, contribute (upload) or manage access on a container.",
    previewHelp: "Pick a user to see exactly what they may read: the same calculation the system uses on every request.",
  },
  people: {
    one: "Add one person",
    csv: "Or paste a list (CSV: email,name,role; several roles separated by ;)",
    import: "Import",
    tempShown: "Temporary passwords (shown once; each person must change theirs at the first login):",
  },
  knowledge: {
    pick: "Container",
    progress: "Ingestion progress",
  },
  check: {
    user: "Ask as",
    question: "Question",
    ask: "Ask",
    hiddenAnswer: "The answer text is not shown after setup: an administrator is not a reader of other people's data. Sources, model and classification are shown.",
  },
  tools: {
    mcp: "MCP registry: register remote MCP servers, approve tools one by one and grant them to roles.",
    router: "Model router: off by default. Turn it on later to pick a model per question among the permitted ones.",
  },
  finish: {
    report: "Download the setup report (Markdown, no secrets)",
    finish: "Finish setup",
    privateNetwork: "This is a demo installation with published passwords: keep it on localhost or a private network.",
    knownAdmin: "An administrator has a known or temporary password. Production setups cannot be finished until it is changed.",
  },
  nav: { back: "Back", next: "Continue", skip: "Skip this step", done: "Mark as done" },
  password: {
    title: "Choose your own password",
    why: "Your password was set by an administrator. Choose a new one to continue (at least 12 characters, not your email address).",
    current: "Current (temporary) password",
    next: "New password",
    repeat: "Repeat the new password",
    save: "Save and continue",
    mismatch: "The two passwords differ.",
  },
  checklist: {
    title: "Setup checklist",
    dismiss: "Dismiss",
    open: "Open",
  },
  assistant: {
    title: "Setup assistant",
    intro: "Re-open any setup step with your normal admin session. Changes take effect immediately and are audited.",
    removeDemo: "Remove demo data",
    removeConfirm: "This permanently deletes the following demo data (the audit log is kept):",
  },
} as const;

export function strength(pw: string): number {
  if (pw.length < 12) return 0;
  let score = 1;
  if (pw.length >= 16) score++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
  if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) score++;
  if (new Set(pw).size < 6) score = 1;
  return Math.min(score, 4);
}

export function bytes(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} kB`;
  return `${n} B`;
}
