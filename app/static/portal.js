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
  if (ifsc) ifsc.addEventListener("input", function () {
    ifsc.value = ifsc.value.toUpperCase().replace(/\s/g, "");
    if (!ifsc.value) return hint(ifsc, "11 characters, for example SBIN0001234.");
    if (/^[A-Z0-9]{11}$/.test(ifsc.value)) return hint(ifsc, "Looks good.", "good");
    hint(ifsc, ifsc.value.length + " of 11 characters entered.", "bad");
  });

  var account = $("input[name=bank_account]");
  if (account) account.addEventListener("input", function () {
    var v = account.value.replace(/[\s-]/g, "");
    if (!v) return hint(account, "Digits only, 9 to 18 digits.");
    if (/^\d{9,18}$/.test(v)) return hint(account, "Looks good.", "good");
    hint(account, "Digits only, 9 to 18 digits (" + v.length + " entered).", "bad");
  });

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
