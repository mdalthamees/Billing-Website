function whatsappReceiptUrl(message, recipient) {
  const phone = String(recipient || "").replace(/\D/g, "");
  const destination = phone ? "https://wa.me/" + phone : "https://wa.me/";
  return destination + "?text=" + encodeURIComponent(message);
}
