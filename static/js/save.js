// Handing a finished file to the person, whatever their browser does with one.
//
// Everywhere else a hidden <a download> is right. iOS Safari ignores the
// download attribute and navigates to the file instead, which throws away the
// editor (and the open document) to show a PDF viewer. There the share sheet is
// the way out: it offers "Save to Files", Mail, AirDrop and the rest, and the
// page stays where it is.

const IOS = /iP(hone|ad|od)/.test(navigator.userAgent)
  || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

function viaLink(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(url);
    a.remove();
  }, 60000);
}

/** Save `blob` as `filename`. Resolves once the browser has taken it over. */
export async function saveBlob(blob, filename) {
  if (IOS && navigator.canShare) {
    const file = new File([blob], filename, { type: blob.type || 'application/pdf' });
    if (navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({ files: [file] });
        return;
      } catch (err) {
        // the person closed the sheet: their choice, and nothing left to do
        if (err?.name === 'AbortError') return;
        // anything else (no gesture left, sharing refused) falls through
      }
    }
  }
  viaLink(blob, filename);
}
