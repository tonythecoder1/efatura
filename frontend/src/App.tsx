import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent, KeyboardEvent } from "react";

const API_URL = (import.meta.env.VITE_API_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const MAX_FILES = 10;

type Party = { name: string | null; tax_id: string | null; address: string | null; email: string | null };
type Tax = { label: string | null; rate: string | null; amount: string | null };
type Invoice = {
  language: string;
  document_type: string | null;
  invoice_number: string | null;
  issue_date: string | null;
  due_date: string | null;
  supplier: Party;
  customer: Party;
  currency: string | null;
  subtotal: string | null;
  discount_total: string | null;
  tax_total: string | null;
  total: string | null;
  amount_due: string | null;
  payment_method: string | null;
  iban: string | null;
  purchase_order: string | null;
  taxes: Tax[];
  notes: string | null;
};
type BatchFileResult = {
  filename: string;
  status: "processed" | "duplicate" | "error";
  duplicate: boolean;
  detail: string | null;
  record: { needs_review: boolean; warnings: string[]; invoice: Invoice } | null;
  csv_content: string | null;
};
type BatchUploadResult = {
  mode: "combined" | "separate";
  total: number;
  processed: number;
  duplicates: number;
  failed: number;
  results: BatchFileResult[];
};
type CsvImportResult = { imported: number; duplicates: number; total: number };
type UploadLimits = { accepted_formats: string[]; max_upload_mb: number; max_pages: number; auth_enabled?: boolean; free_invoice_limit?: number };
type User = { id: string; email: string; plan: string; subscription_status: string; usage_count: number };
type BillingStatus = { plan: string; subscription_status: string; used: number; free_limit: number; remaining_free: number; subscribed: boolean };
type AppStatus = "idle" | "selected" | "importing" | "processing" | "success" | "error";
type ExportMode = "combined" | "separate";

const summaryFields = (invoice: Invoice): Array<[string, string | null]> => [
  ["Tipo de documento", invoice.document_type],
  ["Número da fatura", invoice.invoice_number],
  ["Data de emissão", invoice.issue_date],
  ["Fornecedor", invoice.supplier.name],
  ["NIF do fornecedor", invoice.supplier.tax_id],
  ["Cliente", invoice.customer.name],
  ["Total", invoice.total && invoice.currency ? `${invoice.total} ${invoice.currency}` : invoice.total],
  ["Método de pagamento", invoice.payment_method],
];

function formatFileSize(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function displayValue(value: string | null | undefined) {
  return value?.trim() || "Não identificado";
}

function formatError(detail: unknown, status: number) {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (typeof item === "object" && item && "msg" in item ? String(item.msg) : ""))
      .filter(Boolean);
    if (messages.length) return messages.join(" ");
  }
  if (status === 401) return "A API pede uma chave de acesso local. Verifique a configuração do backend.";
  if (status === 413) return "O ficheiro ultrapassa o limite permitido pela API.";
  if (status === 415) return "Selecione um ficheiro com o formato aceite.";
  if (status === 422) return "Os dados enviados não correspondem ao formato esperado.";
  if (status >= 500) return "A API não conseguiu concluir o processamento. Tente novamente.";
  return "Não foi possível processar este pedido.";
}

async function responseDetail(response: Response) {
  if ((response.headers.get("content-type") || "").includes("application/json")) {
    return ((await response.json()) as { detail?: unknown }).detail;
  }
  return await response.text();
}

function apiHeaders(token: string | null): HeadersInit {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function DownloadIcon() {
  return (
    <svg className="small-icon" viewBox="0 0 20 20" aria-hidden="true">
      <path d="M10 3v10m0 0 3.5-3.5M10 13 6.5 9.5M4 15.5v1A1.5 1.5 0 0 0 5.5 18h9a1.5 1.5 0 0 0 1.5-1.5v-1" />
    </svg>
  );
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function csvFilename(item: BatchFileResult) {
  const base = item.record?.invoice.invoice_number || item.filename.replace(/\.pdf$/i, "") || "fatura";
  return `${base.replace(/[^a-zA-Z0-9._-]+/g, "-")}.csv`;
}

function InvoicePreview({ item, onDownload }: { item: BatchFileResult; onDownload: (item: BatchFileResult) => void }) {
  const invoice = item.record?.invoice;
  if (!invoice) {
    return (
      <article className="batch-item batch-item-error">
        <div className="batch-item-header"><strong>{item.filename}</strong><span className="item-status item-status-error">Não processada</span></div>
        <p>{item.detail || "Não foi possível ler esta fatura."}</p>
      </article>
    );
  }
  return (
    <article className="batch-item">
      <div className="batch-item-header">
        <div><strong>{item.filename}</strong><span className="item-subtitle">{displayValue(invoice.invoice_number)}</span></div>
        <div className="item-actions">
          <span className={`item-status ${item.duplicate ? "item-status-duplicate" : "item-status-success"}`}>{item.duplicate ? "Já existente" : "Processada"}</span>
          {item.csv_content && <button type="button" className="item-download" onClick={() => onDownload(item)}><DownloadIcon /> CSV</button>}
        </div>
      </div>
      <div className="mini-table-wrap">
        <table className="mini-table">
          <caption className="visually-hidden">Pré-visualização de {item.filename}</caption>
          <tbody>
            {summaryFields(invoice).slice(0, 6).map(([label, value]) => (
              <tr key={`${item.filename}-${label}`}><th scope="row">{label}</th><td>{displayValue(value)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      {invoice.taxes.length > 0 && <p className="tax-line">Impostos: {invoice.taxes.map((tax) => [tax.label, tax.rate ? `${tax.rate}%` : "", tax.amount].filter(Boolean).join(" ")).join("; ")}</p>}
    </article>
  );
}

function App() {
  const [files, setFiles] = useState<File[]>([]);
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [batchResult, setBatchResult] = useState<BatchUploadResult | null>(null);
  const [importResult, setImportResult] = useState<CsvImportResult | null>(null);
  const [limits, setLimits] = useState<UploadLimits | null>(null);
  const [mode, setMode] = useState<ExportMode>("combined");
  const [status, setStatus] = useState<AppStatus>("idle");
  const [error, setError] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const [apiUnavailable, setApiUnavailable] = useState(false);
  const [token, setToken] = useState(() => window.localStorage.getItem("efatura_token"));
  const [user, setUser] = useState<User | null>(null);
  const [billing, setBilling] = useState<BillingStatus | null>(null);
  const [categories, setCategories] = useState<string[]>([]);
  const [category, setCategory] = useState("");
  const [costCenter, setCostCenter] = useState("");
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const pdfInputRef = useRef<HTMLInputElement>(null);
  const csvInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/health`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("health");
        setLimits((await response.json()) as UploadLimits);
      })
      .catch(() => {
        if (!controller.signal.aborted) setApiUnavailable(true);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!token) return;
    const headers = apiHeaders(token);
    Promise.all([
      fetch(`${API_URL}/v1/auth/me`, { headers }),
      fetch(`${API_URL}/v1/billing/status`, { headers }),
      fetch(`${API_URL}/v1/categories`, { headers }),
    ]).then(async ([meResponse, billingResponse, categoriesResponse]) => {
      if (!meResponse.ok) throw new Error("session");
      const me = (await meResponse.json()) as { user: User };
      setUser(me.user);
      if (billingResponse.ok) setBilling((await billingResponse.json()) as BillingStatus);
      if (categoriesResponse.ok) setCategories((await categoriesResponse.json()).categories as string[]);
    }).catch(() => {
      window.localStorage.removeItem("efatura_token");
      setToken(null);
      setUser(null);
    });
  }, [token]);

  const authenticate = async () => {
    setAuthBusy(true); setAuthError("");
    try {
      const response = await fetch(`${API_URL}/v1/auth/${authMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!response.ok) throw new Error(formatError(await responseDetail(response), response.status));
      const data = (await response.json()) as { token: string; user: User };
      window.localStorage.setItem("efatura_token", data.token);
      setToken(data.token); setUser(data.user); setPassword("");
    } catch (caught) {
      setAuthError(caught instanceof Error ? caught.message : "Não foi possível iniciar sessão.");
    } finally { setAuthBusy(false); }
  };

  const logout = () => {
    window.localStorage.removeItem("efatura_token");
    setToken(null); setUser(null); setBilling(null);
  };

  const resetResults = () => { setBatchResult(null); setImportResult(null); setDownloadError(""); };
  const clearFiles = () => {
    setFiles([]); resetResults(); setError(""); setStatus("idle");
    if (pdfInputRef.current) pdfInputRef.current.value = "";
  };
  const addFiles = (candidates: File[]) => {
    if (!candidates.length) return;
    setError(""); resetResults();
    if (files.length + candidates.length > MAX_FILES) { setStatus("error"); setError(`Pode selecionar no máximo ${MAX_FILES} faturas de cada vez.`); return; }
    const invalid = candidates.find((candidate) => !candidate.name.toLowerCase().endsWith(".pdf"));
    if (invalid) { setStatus("error"); setError(`"${invalid.name}" não é um PDF. Escolha apenas faturas em PDF.`); return; }
    const tooLarge = limits && candidates.find((candidate) => candidate.size > limits.max_upload_mb * 1024 * 1024);
    if (tooLarge && limits) { setStatus("error"); setError(`"${tooLarge.name}" ultrapassa o limite de ${limits.max_upload_mb} MB.`); return; }
    setFiles((current) => [...current, ...candidates]); setStatus("selected");
  };
  const removeFile = (index: number) => {
    setFiles((current) => current.filter((_, currentIndex) => currentIndex !== index));
    resetResults(); setStatus(files.length === 1 ? "idle" : "selected");
  };
  const onPdfInputChange = (event: ChangeEvent<HTMLInputElement>) => { addFiles(Array.from(event.target.files || [])); event.target.value = ""; };
  const onCsvInputChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = event.target.files?.[0];
    if (!selected) return;
    if (!selected.name.toLowerCase().endsWith(".csv")) { setError("Selecione um ficheiro CSV criado pela aplicação."); setStatus("error"); return; }
    setCsvFile(selected); setImportResult(null); setError("");
    if (csvInputRef.current) csvInputRef.current.value = "";
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => { event.preventDefault(); setDragActive(false); addFiles(Array.from(event.dataTransfer.files)); };
  const openPdfPicker = () => pdfInputRef.current?.click();
  const onDropzoneKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openPdfPicker(); }
  };

  const importCsv = async () => {
    if (!csvFile || status === "importing" || status === "processing") return;
    setStatus("importing"); setError("");
    const formData = new FormData(); formData.append("csv_file", csvFile);
    try {
      const response = await fetch(`${API_URL}/v1/invoices/import-csv`, { method: "POST", body: formData, headers: apiHeaders(token) });
      if (!response.ok) throw new Error(formatError(await responseDetail(response), response.status));
      setImportResult((await response.json()) as CsvImportResult); setCsvFile(null); setStatus(files.length ? "selected" : "success");
    } catch (caught) { setStatus("error"); setError(caught instanceof Error ? caught.message : "Não foi possível importar o CSV."); }
  };

  const processBatch = async () => {
    if (!files.length || status === "processing" || status === "importing") return;
    setStatus("processing"); setError(""); resetResults();
    const formData = new FormData(); files.forEach((file) => formData.append("files", file)); formData.append("mode", mode);
    if (category) formData.append("category", category);
    if (costCenter) formData.append("cost_center", costCenter);
    try {
      const response = await fetch(`${API_URL}/v1/invoices/batch`, { method: "POST", body: formData, headers: apiHeaders(token) });
      if (!response.ok) throw new Error(formatError(await responseDetail(response), response.status));
      setBatchResult((await response.json()) as BatchUploadResult); setStatus("success");
      if (token) {
        const billingResponse = await fetch(`${API_URL}/v1/billing/status`, { headers: apiHeaders(token) });
        if (billingResponse.ok) setBilling((await billingResponse.json()) as BillingStatus);
      }
    } catch (caught) { setStatus("error"); setError(caught instanceof Error ? caught.message : "Não foi possível contactar a API."); }
  };

  const downloadCombinedCsv = async () => {
    setDownloadError("");
    try {
      const response = await fetch(`${API_URL}/v1/invoices.csv`, { headers: apiHeaders(token) });
      if (!response.ok) throw new Error(formatError(await responseDetail(response), response.status));
      downloadBlob(await response.blob(), "faturas.csv");
    } catch (caught) { setDownloadError(caught instanceof Error ? caught.message : "Não foi possível descarregar o CSV."); }
  };
  const downloadSeparateCsv = (item: BatchFileResult) => {
    if (item.csv_content) downloadBlob(new Blob([item.csv_content], { type: "text/csv;charset=utf-8" }), csvFilename(item));
  };

  const fileLimitText = limits ? `PDF · até ${limits.max_upload_mb} MB por ficheiro · máximo ${limits.max_pages} páginas` : "PDF · limites definidos pela API";
  const formatText = limits?.accepted_formats.map((format) => format.toUpperCase()).join(", ") || "PDF";
  const actionLabel = files.length === 1 ? "Processar fatura" : `Processar ${files.length} faturas`;

  const checkout = async (interval: "monthly" | "weekly") => {
    try {
      const response = await fetch(`${API_URL}/v1/billing/checkout`, {
        method: "POST", headers: { ...apiHeaders(token), "Content-Type": "application/json" },
        body: JSON.stringify({ interval }),
      });
      if (!response.ok) throw new Error(formatError(await responseDetail(response), response.status));
      const data = (await response.json()) as { url: string };
      window.location.href = data.url;
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Não foi possível abrir o pagamento."); }
  };

  if (limits?.auth_enabled && !token) {
    return (
      <div className="app-shell"><header className="site-header"><span className="brand-name">Faturas</span><span className="header-note">Leitura documental</span></header>
        <main className="main-content"><section className="upload-card auth-card"><p className="section-kicker">ACESSO À CONTA</p><h1>{authMode === "login" ? "Inicie sessão" : "Crie a sua conta"}</h1><p className="hero-copy">As primeiras 10 faturas são gratuitas.</p>
          <label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" /></label>
          <label>Palavra-passe<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={authMode === "login" ? "current-password" : "new-password"} /></label>
          {authError && <div className="alert error-alert" role="alert">{authError}</div>}
          <button type="button" className="primary-button" onClick={authenticate} disabled={authBusy}>{authBusy ? "A validar…" : authMode === "login" ? "Entrar" : "Criar conta"}</button>
          <button type="button" className="text-button" onClick={() => { setAuthMode(authMode === "login" ? "register" : "login"); setAuthError(""); }}>{authMode === "login" ? "Ainda não tenho conta" : "Já tenho conta"}</button>
        </section></main></div>
    );
  }

  return (
    <div className="app-shell">
      <header className="site-header"><a className="brand" href="/" aria-label="Faturas, início"><img className="company-logo" src="/assets/savannah-logo.png" alt="Savanha" /><span className="brand-divider" aria-hidden="true" /><span className="brand-name">Faturas</span><span className="brand-by">by savanha</span></a><span className="header-note">Leitura documental</span>{user && <div className="account-menu"><span>{user.email}</span><button type="button" className="text-button" onClick={logout}>Sair</button></div>}</header>
      <main className="main-content">
        <section className="hero" aria-labelledby="page-title"><p className="eyebrow">ORGANIZAÇÃO SEM RUÍDO</p><h1 id="page-title">Das suas faturas para uma folha de cálculo.</h1><p className="hero-copy">Carregue uma ou várias faturas e obtenha os dados organizados, prontos a exportar.</p></section>
        <section className="workspace" aria-label="Processar faturas">
          <div className="upload-card">
            <div className="section-heading"><div><p className="section-kicker">01 · DOCUMENTOS</p><h2>Escolha as faturas</h2></div><span className="step-number">01</span></div>
            <div className={`dropzone${dragActive ? " is-dragging" : ""}${files.length ? " has-file" : ""}`} onDragEnter={(event) => { event.preventDefault(); setDragActive(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={(event) => { if (event.currentTarget === event.target) setDragActive(false); }} onDrop={onDrop} onClick={openPdfPicker} onKeyDown={onDropzoneKeyDown} role="button" tabIndex={0} aria-label="Selecionar uma ou várias faturas PDF">
              <input ref={pdfInputRef} className="visually-hidden" type="file" accept="application/pdf,.pdf" multiple onChange={onPdfInputChange} aria-label="Selecionar faturas PDF" />
              <span className="upload-icon" aria-hidden="true"><svg viewBox="0 0 32 32" role="presentation"><path d="M16 22V7m0 0-5.5 5.5M16 7l5.5 5.5" /><path d="M7 20v4.5A2.5 2.5 0 0 0 9.5 27h13a2.5 2.5 0 0 0 2.5-2.5V20" /></svg></span>
              <strong>{files.length ? `${files.length} ${files.length === 1 ? "fatura selecionada" : "faturas selecionadas"}` : "Arraste aqui as suas faturas"}</strong><span>{files.length ? "Pode acrescentar mais documentos até ao limite de 10" : "ou selecione até 10 PDFs do seu dispositivo"}</span><span className="select-button">Selecionar faturas</span>
            </div>
            {files.length > 0 && <div className="file-list" aria-label="Faturas selecionadas">{files.map((file, index) => <div className="file-row" key={`${file.name}-${file.size}-${file.lastModified}-${index}`}><span className="file-type" aria-hidden="true">PDF</span><div className="file-meta"><strong title={file.name}>{file.name}</strong><span>{formatFileSize(file.size)}</span></div><button type="button" className="remove-button" onClick={() => removeFile(index)} aria-label={`Remover ${file.name}`}>Remover</button></div>)}<div className="file-list-footer"><span>{files.length}/{MAX_FILES} documentos</span><button type="button" className="text-button" onClick={clearFiles}>Limpar seleção</button></div></div>}
            <div className="upload-contract" aria-label="Formatos e limites"><span className="contract-icon" aria-hidden="true">i</span><span>Formato aceite: <strong>{formatText}</strong> · {fileLimitText} · uma transação por ficheiro</span></div>
            <div className="csv-import"><div className="csv-import-copy"><p className="section-kicker">BASE EXISTENTE</p><strong>Já tem um CSV da aplicação?</strong><span>Importe-o para juntar novas faturas sem repetir linhas.</span></div><input ref={csvInputRef} className="visually-hidden" type="file" accept="text/csv,.csv" onChange={onCsvInputChange} aria-label="Selecionar CSV existente" /><button type="button" className="secondary-button" onClick={() => csvInputRef.current?.click()} disabled={status === "processing" || status === "importing"}>Escolher CSV</button></div>
            {csvFile && <div className="csv-selected"><span className="file-type csv-type" aria-hidden="true">CSV</span><div className="file-meta"><strong>{csvFile.name}</strong><span>{formatFileSize(csvFile.size)}</span></div><button type="button" className="secondary-button" onClick={importCsv} disabled={status === "importing"}>{status === "importing" ? "A importar…" : "Adicionar ao ficheiro"}</button></div>}
            {importResult && <div className="import-summary" role="status"><span className="success-dot" aria-hidden="true" />{importResult.imported} {importResult.imported === 1 ? "linha adicionada" : "linhas adicionadas"}; {importResult.duplicates} repetidas ignoradas.</div>}
            <div className="classification-fields"><label>Categoria<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">Sem categoria</option>{categories.map((value) => <option key={value} value={value}>{value}</option>)}</select></label><label>Centro de custo<input value={costCenter} onChange={(event) => setCostCenter(event.target.value)} placeholder="Ex.: Marketing" /></label></div>
            <fieldset className="mode-selector"><legend>Como pretende exportar?</legend><label className={`mode-option${mode === "combined" ? " is-selected" : ""}`}><input type="radio" name="export-mode" value="combined" checked={mode === "combined"} onChange={() => setMode("combined")} /><span><strong>Um CSV combinado</strong><small>Todas as faturas ficam no ficheiro acumulado.</small></span></label><label className={`mode-option${mode === "separate" ? " is-selected" : ""}`}><input type="radio" name="export-mode" value="separate" checked={mode === "separate"} onChange={() => setMode("separate")} /><span><strong>Um CSV por fatura</strong><small>Recebe um ficheiro independente para cada documento.</small></span></label></fieldset>
            {billing && <div className="billing-panel"><strong>{billing.subscribed ? `Plano ${billing.plan}` : `${billing.remaining_free} de ${billing.free_limit} faturas gratuitas restantes`}</strong>{!billing.subscribed && billing.remaining_free === 0 && <div className="billing-actions"><button type="button" className="secondary-button" onClick={() => checkout("monthly")}>Plano mensal</button><button type="button" className="secondary-button" onClick={() => checkout("weekly")}>Plano semanal</button></div>}</div>}
            {apiUnavailable && <p className="inline-note" role="status">Não foi possível ler os limites da API. O processamento continua sujeito às regras do backend.</p>}
            {error && <div className="alert error-alert" role="alert"><span className="alert-mark" aria-hidden="true">!</span><span>{error}</span></div>}
            <button type="button" className="primary-button" onClick={processBatch} disabled={!files.length || status === "processing" || status === "importing" || Boolean(billing && !billing.subscribed && billing.remaining_free === 0)}>{status === "processing" ? "A processar faturas…" : actionLabel}<span aria-hidden="true">→</span></button>
            {status === "processing" && <p className="processing-note" role="status" aria-live="polite">A análise pode demorar alguns instantes por documento.</p>}
          </div>
          <aside className="side-panel" aria-label="Como funciona"><p className="section-kicker">02 · RESULTADO</p><h2>Dados com contexto.</h2><p>Importe uma base existente, processe até 10 PDFs e escolha como quer receber os dados.</p><div className="side-rule" /><div className="side-detail"><span className="detail-dot" aria-hidden="true" /><span>Linhas repetidas são ignoradas</span></div><div className="side-detail"><span className="detail-dot" aria-hidden="true" /><span>Até 10 faturas por lote</span></div><div className="side-detail"><span className="detail-dot" aria-hidden="true" /><span>Português e inglês</span></div></aside>
        </section>
        {batchResult && <section className="result-card" aria-labelledby="result-title"><div className="result-heading"><div><p className="section-kicker">03 · REVISÃO</p><h2 id="result-title">Lote processado</h2><p className="result-status"><span className="success-dot" aria-hidden="true" />{batchResult.processed} novas · {batchResult.duplicates} repetidas · {batchResult.failed} com erro</p></div>{batchResult.mode === "combined" && <button type="button" className="download-button" onClick={downloadCombinedCsv}><DownloadIcon /> Descarregar CSV combinado</button>}</div><div className="batch-overview"><strong>{batchResult.total} documentos recebidos</strong><span>{batchResult.mode === "combined" ? "O ficheiro acumulado foi atualizado." : "Cada documento tem um CSV independente disponível abaixo."}</span></div><div className="batch-results">{batchResult.results.map((item) => <InvoicePreview key={`${item.filename}-${item.record?.invoice.invoice_number || item.detail}`} item={item} onDownload={downloadSeparateCsv} />)}</div>{downloadError && <p className="download-error" role="alert">{downloadError}</p>}</section>}
      </main>
      <footer className="site-footer"><span>Dados organizados para decisões mais claras.</span><span>by savanha</span></footer>
    </div>
  );
}

export default App;
