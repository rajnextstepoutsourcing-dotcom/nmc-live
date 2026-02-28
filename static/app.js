function setStatus(msg) {
  const box = document.getElementById('statusBox');
  if (box) box.textContent = msg;
}

function setExtractHint(msg) {
  const el = document.getElementById('extractHint');
  if (el) el.textContent = msg || '';
}

function getFile() {
  const input = document.getElementById('fileInput');
  return (input && input.files && input.files[0]) ? input.files[0] : null;
}

async function extractPin() {
  const file = getFile();
  if (!file) { setStatus('Please choose a file first.'); return; }

  // Reset UI state for a fresh extraction
  setExtractHint('');
  setStatus('Extracting PIN…');
  const pinInput = document.getElementById('pinInput');
  const btnRun = document.getElementById('btnRun');
  if (pinInput) { pinInput.value = ''; pinInput.disabled = true; }
  if (btnRun) btnRun.disabled = true;

  const fd = new FormData();
  fd.append('file', file);

  try {
    const res = await fetch('/extract', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      setStatus(data.detail || 'Extraction failed.');
      return;
    }

    const pin = (data.nmc_pin || '').trim();
    if (!pin) {
      setStatus('Could not find an NMC PIN in this file. Try a clearer document or edit manually.');
      if (pinInput) { pinInput.disabled = false; pinInput.focus(); }
      if (btnRun) btnRun.disabled = false;
      return;
    }

    if (pinInput) { pinInput.value = pin; pinInput.disabled = false; }
    if (btnRun) btnRun.disabled = false;

    setStatus('PIN extracted. Please review/edit if needed, then click “Run NMC Check & Download PDF”.');
  } catch (e) {
    setStatus('Extraction failed (network error).');
  }
}

async function runCheck() {
  const file = getFile();
  if (!file) { setStatus('Please choose a file first.'); return; }

  const pin = (document.getElementById('pinInput')?.value || '').trim();
  if (!pin) { setStatus('Please enter an NMC PIN.'); return; }

  setStatus('Running NMC check… this may take 30–90 seconds.');

  try {
    const res = await fetch('/run-pin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nmc_pin: pin })
    });

    const blob = await res.blob();
    const isPdf = (blob.type || '').includes('pdf') || (res.headers.get('content-type') || '').includes('pdf');

    if (!res.ok && !isPdf) {
      const text = await blob.text().catch(() => '');
      setStatus(text || 'Run failed.');
      return;
    }

    // Always download whatever comes back (success PDF or error PDF)
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;

    const cd = res.headers.get('content-disposition') || '';
    const match = cd.match(/filename="?([^";]+)"?/i);
    a.download = match ? match[1] : 'nmc-result.pdf';

    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);

    setStatus('Done. PDF downloaded.');
  } catch (e) {
    setStatus('Run failed (network error).');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btnExtract')?.addEventListener('click', extractPin);
  document.getElementById('btnRun')?.addEventListener('click', runCheck);

  // Ensure initial state is clean
  setStatus('Waiting…');
  setExtractHint('');
});
