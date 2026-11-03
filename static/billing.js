let cart = [];
let searchRequest = 0;
let pendingBillKey = newBillKey();

const money = (value) => "₹" + Number(value || 0).toFixed(2);

function newBillKey() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function setBillingStatus(message, isError = false) {
  const status = document.getElementById("billing-status");
  status.textContent = message;
  status.classList.toggle("is-error", isError);
}

function showProductSkeleton(box) {
  box.innerHTML = Array.from({ length: 4 }, () =>
    '<div class="product-skeleton"><span class="skeleton skeleton-thumb"></span>' +
    '<span class="skeleton-copy"><i class="skeleton skeleton-line"></i>' +
    '<i class="skeleton skeleton-line short"></i></span></div>'
  ).join("");
}

async function searchProducts() {
  const query = document.getElementById("search").value;
  const department = document.getElementById("dept").value;
  const category = document.getElementById("category").value;
  const request = ++searchRequest;
  const box = document.getElementById("productResults");
  box.setAttribute("aria-busy", "true");
  showProductSkeleton(box);
  try {
    const params = new URLSearchParams({ q: query, department, category });
    const response = await fetch(`/api/products?${params}`);
    if (!response.ok) throw new Error("Products could not be loaded. Please try again.");
    const products = await response.json();
    if (request !== searchRequest) return;
    box.replaceChildren();
    if (!products.length) {
      box.innerHTML = '<p class="muted p-3">No available products match these filters.</p>';
      return;
    }
    products.forEach((product) => {
      const button = document.createElement("button");
      const details = document.createElement("span");
      const name = document.createElement("b");
      const meta = document.createElement("small");
      const price = document.createElement("strong");
      button.className = "product-result";
      button.type = "button";
      name.textContent = product.name;
      meta.textContent = `${product.code} · ${product.department} · ` +
        `${product.subcategory || product.category} · ${product.size || "One size"} · ` +
        `${product.colour || "—"} · Stock ${product.stock}`;
      price.textContent = money(product.selling_price);
      details.append(name, meta);
      button.append(details, price);
      button.addEventListener("click", () => addProduct(product));
      box.appendChild(button);
    });
  } catch (error) {
    if (request === searchRequest) {
      const message = document.createElement("p");
      message.className = "text-danger p-3";
      message.textContent = error.message;
      box.replaceChildren(message);
    }
  } finally {
    if (request === searchRequest) box.removeAttribute("aria-busy");
  }
}

function addProduct(product) {
  const existing = cart.find((item) => item.id === product.id);
  if (existing) {
    if (existing.quantity >= product.stock) {
      setBillingStatus(`Only ${product.stock} units of ${product.name} are available.`, true);
      return;
    }
    existing.quantity += 1;
  } else {
    cart.push({ ...product, quantity: 1, price: Number(product.selling_price) });
  }
  setBillingStatus("");
  renderCart();
}

function renderCart() {
  const box = document.getElementById("cartItems");
  box.replaceChildren();
  if (!cart.length) {
    box.innerHTML = '<p class="muted">No items added yet.</p>';
  }
  cart.forEach((product, index) => {
    const row = document.createElement("div");
    row.className = "cart-row";
    const details = document.createElement("div");
    const name = document.createElement("b");
    const meta = document.createElement("small");
    const quantity = document.createElement("div");
    quantity.className = "qty";
    name.textContent = product.name;
    meta.textContent = `${product.size || "One size"} · ${product.colour || "—"} · ` +
      `${money(product.price)} each`;
    details.append(name, meta);

    const decrement = document.createElement("button");
    decrement.type = "button";
    decrement.dataset.index = index;
    decrement.dataset.delta = "-1";
    decrement.textContent = "−";
    decrement.setAttribute("aria-label", `Decrease ${product.name} quantity`);
    decrement.disabled = product.quantity <= 1;
    const count = document.createElement("b");
    count.textContent = product.quantity;
    const increment = document.createElement("button");
    increment.type = "button";
    increment.dataset.index = index;
    increment.dataset.delta = "1";
    increment.textContent = "+";
    increment.disabled = product.quantity >= Number(product.stock);
    increment.setAttribute("aria-label", `Increase ${product.name} quantity`);
    if (increment.disabled) increment.title = `Maximum available stock (${product.stock}) reached`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "remove";
    remove.dataset.remove = index;
    remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove ${product.name}`);
    quantity.append(decrement, count, increment, remove);
    row.append(details, quantity);
    box.appendChild(row);
  });

  const subtotal = cart.reduce((sum, product) => sum + product.quantity * product.price, 0);
  const discountInput = document.getElementById("discount");
  const receivedInput = document.getElementById("received");
  const discount = Number(discountInput.value);
  const received = Number(receivedInput.value);
  const total = subtotal - discount;
  const invalidDiscount = !Number.isFinite(discount) || discount < 0 || discount > subtotal;
  const invalidReceived = !Number.isFinite(received) || received < 0 || received > Math.max(total, 0);
  document.getElementById("subtotal").textContent = money(subtotal);
  document.getElementById("total").textContent = money(Math.max(total, 0));
  document.getElementById("balance").textContent = money(Math.max(total - received, 0));

  const completeButton = document.getElementById("generate");
  completeButton.disabled = !cart.length || invalidDiscount || invalidReceived;
  if (invalidDiscount) setBillingStatus("Discount must not exceed the subtotal.", true);
  else if (invalidReceived) setBillingStatus("Amount received must not exceed the grand total.", true);
  else if (document.getElementById("billing-status").classList.contains("is-error")) {
    setBillingStatus("");
  }

  box.querySelectorAll("[data-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const product = cart[Number(button.dataset.index)];
      product.quantity = Math.min(
        Number(product.stock),
        Math.max(1, product.quantity + Number(button.dataset.delta))
      );
      renderCart();
    });
  });
  box.querySelectorAll("[data-remove]").forEach((button) => {
    button.addEventListener("click", () => {
      cart.splice(Number(button.dataset.remove), 1);
      renderCart();
    });
  });
}

document.getElementById("search").addEventListener("input", searchProducts);
document.getElementById("dept").addEventListener("change", searchProducts);
document.getElementById("category").addEventListener("change", searchProducts);
["discount", "received"].forEach((id) =>
  document.getElementById(id).addEventListener("input", renderCart)
);

document.getElementById("generate").addEventListener("click", async () => {
  if (!cart.length) {
    setBillingStatus("Add at least one product before completing the sale.", true);
    return;
  }
  const payload = {
    idempotency_key: pendingBillKey,
    items: cart.map((product) => ({ id: product.id, quantity: product.quantity })),
    discount: Number(document.getElementById("discount").value || 0),
    received: Number(document.getElementById("received").value || 0),
    payment_method: document.getElementById("method").value,
    customer_name: document.getElementById("customerName").value,
    customer_phone: document.getElementById("customerPhone").value
  };
  const button = document.getElementById("generate");
  button.disabled = true;
  button.textContent = "Saving bill…";
  let billSaved = false;
  let savedReceiptUrl = "";
  try {
    const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
    const response = await fetch("/api/bills", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "Could not save bill. Please try again.");

    billSaved = true;
    savedReceiptUrl = `/receipt/${data.bill_id}`;
    pendingBillKey = newBillKey();
    const customerName = data.receipt.customerName || "Walk-in Customer";
    const message = `HI-5 receipt for ${customerName}. Bill ${data.bill_no}. ` +
      `Grand total ${money(data.receipt.total)}.`;
    const whatsappMessage = `${message} The receipt image was downloaded; attach it to send the full bill.`;
    const file = await createReceiptImageFile(data.receipt);

    if (navigator.share && navigator.canShare && navigator.canShare({ files: [file] })) {
      try {
        await navigator.share({ files: [file], title: data.bill_no, text: message });
        window.location.assign(savedReceiptUrl);
        return;
      } catch (error) {
        if (error.name === "AbortError") {
          window.location.assign(savedReceiptUrl);
          return;
        }
      }
    }
    downloadReceiptFile(file);
    window.location.href = whatsappReceiptUrl(whatsappMessage);
  } catch (error) {
    if (billSaved) {
      window.location.assign(savedReceiptUrl);
      return;
    }
    setBillingStatus(error.message, true);
    button.disabled = false;
    button.textContent = "Complete sale";
  }
});

renderCart();
