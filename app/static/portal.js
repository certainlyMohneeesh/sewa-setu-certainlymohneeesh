/* Progressive enhancement only: every hint is advisory. Nothing here blocks a submit,
   so the server remains the single source of truth for validation messages. */
(function () {
  function $(sel, root) { return (root || document).querySelector(sel); }

  function hint(input, message, state) {
    var box = document.getElementById(input.id + "-hint");
    if (!box) return;
    box.textContent = message || "";
    box.className = "hint" + (state ? " " + state : "");
    if (state === "bad") input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }

  function parseDob(value) {
    var m = /^(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{4})$/.exec(value.trim());
    if (!m) return null;
    var d = +m[1], mo = +m[2], y = +m[3], dt = new Date(y, mo - 1, d);
    return dt.getFullYear() === y && dt.getMonth() === mo - 1 && dt.getDate() === d ? dt : null;
  }

  var dob = $("input[name=dob]");
  if (dob) dob.addEventListener("input", function () {
    if (!dob.value.trim()) return hint(dob, "Day / Month / Year, for example 05/10/1960.");
    var dt = parseDob(dob.value);
    if (!dt) return hint(dob, "Use DD/MM/YYYY, for example 05/10/1960.", "bad");
    var now = new Date(), age = now.getFullYear() - dt.getFullYear();
    if (now < new Date(now.getFullYear(), dt.getMonth(), dt.getDate())) age--;
    if (age < 60) return hint(dob, "Applicant must be at least 60 years old (age " + age + ").", "bad");
    hint(dob, "Age " + age + ": eligible.", "good");
  });

  var mobile = $("input[name=mobile]");
  if (mobile) mobile.addEventListener("input", function () {
    var digits = mobile.value.replace(/\D/g, "");
    if (digits !== mobile.value) mobile.value = digits;
    if (!digits) return hint(mobile, "10 digits, without +91 or spaces.");
    if (digits.length === 10) return hint(mobile, "Looks good.", "good");
    hint(mobile, digits.length + " of 10 digits entered.", "bad");
  });

  var ifsc = $("input[name=ifsc]");
  if (ifsc) {
    var bankBox = document.getElementById("ifsc-bank"), ifscTimer = null, ifscSeq = 0;
    var showBank = function (text, state) {
      if (!bankBox) return;
      bankBox.textContent = text || "";
      bankBox.className = "bankname" + (state ? " " + state : "");
    };
    var lookupIfsc = function () {
      var code = ifsc.value, seq = ++ifscSeq;
      if (!/^[A-Z]{4}0[A-Z0-9]{6}$/.test(code)) return showBank("");
      showBank("Checking the bank...");
      fetch("/api/ifsc/" + encodeURIComponent(code), { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (info) {
          if (seq !== ifscSeq) return;
          if (!info) return showBank("");
          var parts = [info.bank, info.branch ? info.branch + " Branch" : null, info.city]
            .filter(Boolean);
          if (info.bank) showBank(parts.join(", "), "good");
          else showBank("We could not recognise this bank. Please check the code in your passbook.", "bad");
        })
        .catch(function () { if (seq === ifscSeq) showBank(""); });
    };
    ifsc.addEventListener("input", function () {
      ifsc.value = ifsc.value.toUpperCase().replace(/\s/g, "");
      clearTimeout(ifscTimer);
      showBank("");
      if (!ifsc.value) return hint(ifsc, "11 characters, for example SBIN0001234.");
      if (/^[A-Z0-9]{11}$/.test(ifsc.value)) {
        hint(ifsc, "Looks good.", "good");
        ifscTimer = setTimeout(lookupIfsc, 350);
      } else hint(ifsc, ifsc.value.length + " of 11 characters entered.", "bad");
    });
    if (ifsc.value) lookupIfsc();
  }

  // Show the account number in groups of four, like the passbook. The spaces are removed
  // again before the form is posted, and the server ignores them too.
  var account = $("input[name=bank_account]");
  if (account) {
    var group = function () {
      var caret = account.selectionStart, before = account.value.slice(0, caret).replace(/\D/g, "").length;
      var digits = account.value.replace(/\D/g, "").slice(0, 18);
      account.value = digits.replace(/(\d{4})(?=\d)/g, "$1 ");
      var pos = 0, seen = 0;
      while (pos < account.value.length && seen < before) { if (/\d/.test(account.value[pos])) seen++; pos++; }
      if (document.activeElement === account) account.setSelectionRange(pos, pos);
      var n = digits.length;
      if (!n) return hint(account, "Digits only, 9 to 18 digits.");
      if (n >= 9) return hint(account, n + " digits entered. Compare with your passbook.", "good");
      hint(account, "Digits only, 9 to 18 digits (" + n + " entered).", "bad");
    };
    account.addEventListener("input", group);
    if (account.value) group();
    if (account.form) account.form.addEventListener("submit", function () {
      account.value = account.value.replace(/\s/g, "");
    });
  }

  // Read-aloud: the page speaks to the citizen; nothing is recorded or transcribed.
  // Buttons stay hidden on browsers without speech synthesis.
  var synth = window.speechSynthesis;
  if (synth && window.SpeechSynthesisUtterance) {
    var bar = document.getElementById("voicebar"), langSel = document.getElementById("voice-lang");
    var status = document.getElementById("voice-status");
    var buttons = document.querySelectorAll("button.listen");
    var PREFIX = { en: ["en-in", "en"], hi: ["hi"], as: ["as", "bn"] };
    var saved = null;
    try { saved = localStorage.getItem("voiceLang"); } catch (e) {}
    if (saved && langSel) langSel.value = saved;
    if (bar && buttons.length) bar.hidden = false;
    buttons.forEach(function (b) { b.hidden = false; });

    var pickVoice = function (lang) {
      var voices = synth.getVoices();
      for (var i = 0; i < PREFIX[lang].length; i++) {
        var p = PREFIX[lang][i];
        for (var j = 0; j < voices.length; j++) {
          if (voices[j].lang.replace("_", "-").toLowerCase().indexOf(p) === 0) return voices[j];
        }
      }
      return null;
    };

    if (langSel) langSel.addEventListener("change", function () {
      try { localStorage.setItem("voiceLang", langSel.value); } catch (e) {}
      if (status) status.textContent = "";
    });

    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest && ev.target.closest("button.listen");
      if (!btn) return;
      var lang = langSel ? langSel.value : "en";
      var voice = pickVoice(lang), text = btn.getAttribute("data-say-" + lang);
      if (!voice && lang !== "en") {
        if (status) status.textContent = "This device has no voice for this language. Reading in English.";
        lang = "en"; voice = pickVoice("en"); text = btn.getAttribute("data-say-en");
      } else if (status) status.textContent = "";
      synth.cancel();
      var u = new SpeechSynthesisUtterance(text);
      if (voice) { u.voice = voice; u.lang = voice.lang; }
      u.rate = 0.85;
      u.onstart = function () { btn.classList.add("speaking"); };
      u.onend = u.onerror = function () { btn.classList.remove("speaking"); };
      synth.speak(u);
    });
    if (synth.onvoiceschanged !== undefined) synth.onvoiceschanged = function () {};
  }

  var otp = $("input[name=otp]");
  if (otp) otp.addEventListener("input", function () {
    otp.value = otp.value.replace(/\D/g, "");
  });

  // Husband / late spouse fields depend on marital status. They are hidden, never
  // removed, so the form still posts the same field names.
  var marital = $("select[name=marital_status]");
  if (marital) {
    var nameRow = $("#husband-name-row"), employerRow = $("#husband-employer-row");
    var nameLabel = $("label[for=husband_name]");
    var update = function () {
      var v = marital.value;
      if (nameRow) nameRow.classList.toggle("hidden", v === "Unmarried");
      if (employerRow) employerRow.classList.toggle("hidden", v === "Unmarried" || v === "Widowed");
      if (nameLabel) nameLabel.textContent = v === "Widowed"
        ? "Late Spouse's Name (Optional)" : "Husband's Name (Optional)";
    };
    marital.addEventListener("change", update);
    update();
  }

  var file = $("input[type=file][name=document]");
  if (file) file.addEventListener("change", function () {
    var f = file.files && file.files[0];
    var box = document.getElementById("file-hint");
    var preview = document.getElementById("file-preview");
    if (preview) preview.innerHTML = "";
    if (!f) return;
    var ok = /\.(pdf|jpe?g|png)$/i.test(f.name), size = f.size / 1048576;
    var msg = f.name + " (" + size.toFixed(2) + " MB). ";
    if (!ok) msg += "This is not a PDF or JPG file.";
    else if (f.size > 5 * 1048576) msg += "This file is larger than 5 MB.";
    else msg += "File type and size are fine.";
    box.textContent = msg;
    box.className = "hint " + (ok && f.size <= 5 * 1048576 ? "good" : "bad");
    if (ok && /^image\//.test(f.type) && preview) {
      var img = document.createElement("img");
      img.alt = "Preview of the selected document";
      img.style.maxWidth = "240px";
      img.src = URL.createObjectURL(f);
      preview.appendChild(img);
    }
  });
})();
