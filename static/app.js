const fileInput = document.getElementById('file');
const statusEl = document.getElementById('status');

const step2 = document.getElementById('step2');
const pinInput = document.getElementById('pin');

const btnExtract = document.getElementById('btnExtract');
const btnRun = document.getElementById('btnRun');

function setStatus(msg) {
  statusEl.textContent = msg;
}

function downloadBlob(blob, filename) {
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.URL.revokeObjectURL(url);
}

btnExtract.addEventListener('click', async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) {
    setStatus('Please choose a file.');
    return;
  }

  btnExtract.disabled = true;
  btnRun.disabled = true;
  setStatus('Extracting PIN…');

  try {
    const fd = new FormData();
    fd.append('file', file);

    const res = await fetch('/extract', { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));

    if (!res.ok || !data.ok) {
      setStatus(data.detail || data.error || 'Extraction failed.');
      step2.style.display = 'none';
      return;
    }

    pinInput.value = (data.nmc_pin || '').toUpperCase();
    step2.style.display = 'block';
    setStatus('PIN extracted. Please review/edit, then run the check.');
  } catch (e) {
    setStatus('Extraction error: ' + (e?.message || e));
    step2.style.display = 'none';
  } finally {
    btnExtract.disabled = false;
    btnRun.disabled = false;
  }
});

btnRun.addEventListener('click', async () => {
  const pin = (pinInput.value || '').trim().toUpperCase();
  if (!pin) {
    setStatus('Please enter an NMC PIN.');
    return;
  }

  btnExtract.disabled = true;
  btnRun.disabled = true;
  setStatus('Running NMC check… (this may take a moment)');

  try {
    const res = await fetch('/run-pin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nmc_pin: pin })
    });

    if (!res.ok) {
      // try read JSON error
      const err = await res.json().catch(() => null);
      setStatus(err?.detail || 'Run failed.');
      btnExtract.disabled = false;
      btnRun.disabled = false;
      return;
    }

    const blob = await res.blob();
    const cd = res.headers.get('content-disposition') || '';
    const match = /filename="?([^"]+)"?/i.exec(cd);
    const filename = match ? match[1] : 'NMC-Result.pdf';
    downloadBlob(blob, filename);
    setStatus('Done. PDF downloaded.');
  } catch (e) {
    setStatus('Run error: ' + (e?.message || e));
  } finally {
    btnExtract.disabled = false;
    btnRun.disabled = false;
  }
});
