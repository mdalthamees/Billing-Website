function receiptMoney(value) {
  return "₹" + Number(value || 0).toFixed(2);
}

function receiptText(context, text, x, y, maxWidth, font, lineHeight, color, align) {
  context.font = font;
  context.fillStyle = color || "#202124";
  context.textAlign = align || "left";
  const words = String(text || "").split(/\s+/).filter(Boolean);
  let line = "";
  for (const word of words) {
    const candidate = line ? line + " " + word : word;
    if (line && context.measureText(candidate).width > maxWidth) {
      context.fillText(line, x, y);
      y += lineHeight;
      line = word;
    } else {
      line = candidate;
    }
  }
  if (line) {
    context.fillText(line, x, y);
    y += lineHeight;
  }
  return y;
}

async function createReceiptImageFile(data) {
  const width = 960;
  const padding = 64;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  const detailHeight = data.items.reduce((height, item) => {
    const detail = [item.category, item.size, item.colour].filter(Boolean).join(" · ");
    return height +
      Math.max(0, Math.ceil(item.name.length / 82) - 1) * 24 +
      Math.max(0, Math.ceil(detail.length / 110) - 1) * 20;
  }, 0);
  const addressHeight = Math.max(0, Math.ceil(data.address.length / 88) - 1) * 24;
  canvas.height = Math.max(720, 660 + data.items.length * 106 + detailHeight + addressHeight);
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Your browser could not create the receipt image.");

  context.fillStyle = "#fff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  let y = 52;
  if (data.logoUrl) {
    try {
      const logo = new Image();
      logo.src = data.logoUrl;
      await logo.decode();
      const scale = Math.min(140 / logo.naturalWidth, 84 / logo.naturalHeight);
      context.drawImage(logo, (width - logo.naturalWidth * scale) / 2, y,
        logo.naturalWidth * scale, logo.naturalHeight * scale);
      y += 104;
    } catch (error) {
      console.warn("Shop logo could not be added to the receipt image.", error);
    }
  }

  y = receiptText(context, data.shopName || "HI-5 FOOTWEAR & BOUTIQUES",
    width / 2, y + 20, width - padding * 2, "700 30px Arial, sans-serif",
    38, "#171717", "center");
  y = receiptText(context, data.address, width / 2, y + 2, width - padding * 2,
    "17px Arial, sans-serif", 24, "#626262", "center");
  if (data.shopPhone) {
    y = receiptText(context, data.shopPhone, width / 2, y, width - padding * 2,
      "16px Arial, sans-serif", 23, "#626262", "center");
  }
  y += 12;
  context.font = "700 17px Arial, sans-serif";
  context.fillStyle = "#171717";
  context.textAlign = "center";
  context.fillText("SALES RECEIPT", width / 2, y);
  y += 28;
  context.textAlign = "left";
  y = receiptText(context, "Bill No: " + data.billNumber, padding, y,
    width - padding * 2, "16px Arial, sans-serif", 23);
  y = receiptText(context, "Date: " + data.date.replace("T", " "), padding, y,
    width - padding * 2, "16px Arial, sans-serif", 23);
  y = receiptText(context, "Customer: " + data.customerName, padding, y,
    width - padding * 2, "16px Arial, sans-serif", 23);
  y += 12;

  context.strokeStyle = "#d7dce1";
  context.beginPath();
  context.moveTo(padding, y);
  context.lineTo(width - padding, y);
  context.stroke();
  y += 28;
  for (const item of data.items) {
    y = receiptText(context, item.name, padding, y, width - padding * 2,
      "700 18px Arial, sans-serif", 24);
    const detail = [item.category, item.size, item.colour].filter(Boolean).join(" · ");
    if (detail) {
      y = receiptText(context, detail, padding, y, width - padding * 2,
        "14px Arial, sans-serif", 20, "#626262");
    }
    const qtyPrice = "Qty " + item.quantity + "  ·  " + receiptMoney(item.price) + " each";
    context.font = "15px Arial, sans-serif";
    context.fillStyle = "#424242";
    context.textAlign = "left";
    context.fillText(qtyPrice, padding, y + 2);
    context.font = "700 16px Arial, sans-serif";
    context.textAlign = "right";
    context.fillText(receiptMoney(item.total), width - padding, y + 2);
    y += 32;
    context.strokeStyle = "#ececec";
    context.beginPath();
    context.moveTo(padding, y);
    context.lineTo(width - padding, y);
    context.stroke();
    y += 18;
  }

  y += 8;
  const totals = [
    ["Subtotal", receiptMoney(data.subtotal), false],
    ["Discount", "− " + receiptMoney(data.discount), false],
    ["Grand Total", receiptMoney(data.total), true],
    ["Payment Method", data.paymentMethod, false]
  ];
  for (const [label, value, isGrand] of totals) {
    if (isGrand) {
      context.strokeStyle = "#d7dce1";
      context.beginPath();
      context.moveTo(width - padding - 360, y - 10);
      context.lineTo(width - padding, y - 10);
      context.stroke();
      y += 8;
    }
    context.font = isGrand ? "700 22px Arial, sans-serif" : "16px Arial, sans-serif";
    context.fillStyle = "#202124";
    context.textAlign = "left";
    context.fillText(label, width - padding - 360, y);
    context.textAlign = "right";
    context.fillText(value, width - padding, y);
    y += isGrand ? 42 : 30;
  }

  y += 18;
  context.strokeStyle = "#b9b9b9";
  context.beginPath();
  context.setLineDash([5, 5]);
  context.moveTo(padding, y);
  context.lineTo(width - padding, y);
  context.stroke();
  context.setLineDash([]);
  y += 38;
  context.font = "700 18px Arial, sans-serif";
  context.fillStyle = "#171717";
  context.textAlign = "center";
  context.fillText("Thank you for shopping with us!", width / 2, y);
  y += 28;
  context.fillText("We look forward to seeing you again.", width / 2, y);

  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob((result) => {
      if (result) resolve(result);
      else reject(new Error("Your browser could not encode the receipt image."));
    }, "image/png");
  });
  return new File([blob], data.billNumber + ".png", { type: "image/png" });
}

function downloadReceiptFile(file) {
  const link = document.createElement("a");
  const objectUrl = URL.createObjectURL(file);
  link.href = objectUrl;
  link.download = file.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

function whatsappReceiptUrl(message) {
  return "https://wa.me/918667579407?text=" + encodeURIComponent(message);
}
