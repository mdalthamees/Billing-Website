let cart=[];
let searchRequest=0;
const money=n=>'₹'+Number(n||0).toFixed(2);
function showProductSkeleton(box){
 box.innerHTML=Array.from({length:4},()=>'<div class="product-skeleton"><span class="skeleton skeleton-thumb"></span><span class="skeleton-copy"><i class="skeleton skeleton-line"></i><i class="skeleton skeleton-line short"></i></span></div>').join('');
}
async function searchProducts(){
 const q=document.getElementById('search').value,department=document.getElementById('dept').value;
 const request=++searchRequest,box=document.getElementById('productResults');
 box.setAttribute('aria-busy','true');showProductSkeleton(box);
 try{
 const r=await fetch(`/api/products?q=${encodeURIComponent(q)}&department=${encodeURIComponent(department)}`);
 if(!r.ok)throw new Error('Products could not be loaded. Please try again.');
 const items=await r.json();
 if(request!==searchRequest)return;
 box.innerHTML='';
 if(!items.length){box.innerHTML='<p class="muted p-3">No available products found.</p>';return}
 items.forEach(p=>{const b=document.createElement('button');b.className='product-result';b.innerHTML=`<span><b>${p.name}</b><small>${p.code} · ${p.department} · ${p.size||'One size'} · ${p.colour||'—'} · Stock ${p.stock}</small></span><strong>${money(p.selling_price)}</strong>`;b.onclick=()=>add(p);box.appendChild(b)});
 }catch(error){if(request===searchRequest)box.innerHTML=`<p class="text-danger p-3">${error.message}</p>`}
 finally{if(request===searchRequest)box.removeAttribute('aria-busy')}
}
function add(p){let found=cart.find(x=>x.id===p.id);if(found){if(found.quantity>=p.stock){alert('No more stock available');return}found.quantity++}else cart.push({...p,quantity:1,price:Number(p.selling_price)});render();}
function render(){
 const box=document.getElementById('cartItems');box.innerHTML='';
 if(!cart.length)box.innerHTML='<p class="muted">No items added yet.</p>';
 cart.forEach((p,i)=>{let row=document.createElement('div');row.className='cart-row';row.innerHTML=`<div><b>${p.name}</b><small>${p.size||'One size'} · ${p.colour||'—'} · ${money(p.price)}</small></div><div class="qty"><button data-i="${i}" data-d="-1">−</button><b>${p.quantity}</b><button data-i="${i}" data-d="1">+</button><button class="remove" data-r="${i}">×</button></div>`;box.appendChild(row)});
 const subtotal=cart.reduce((s,p)=>s+p.quantity*p.price,0),discount=Math.max(0,Number(document.getElementById('discount').value||0)),total=Math.max(0,subtotal-discount),received=Math.max(0,Number(document.getElementById('received').value||0));
 document.getElementById('subtotal').textContent=money(subtotal);document.getElementById('total').textContent=money(total);document.getElementById('balance').textContent=money(Math.max(0,total-received));
 box.querySelectorAll('[data-i]').forEach(b=>b.onclick=()=>{let p=cart[+b.dataset.i];p.quantity=Math.max(1,p.quantity+Number(b.dataset.d));render()});
 box.querySelectorAll('[data-r]').forEach(b=>b.onclick=()=>{cart.splice(+b.dataset.r,1);render()});
}
document.getElementById('search').addEventListener('input',searchProducts);document.getElementById('dept').addEventListener('change',searchProducts);
['discount','received'].forEach(id=>document.getElementById(id).addEventListener('input',render));
document.getElementById('generate').onclick=async()=>{
 if(!cart.length){alert('Add at least one product.');return}
 const payload={items:cart.map(p=>({id:p.id,quantity:p.quantity,price:p.price})),discount:Number(document.getElementById('discount').value||0),received:Number(document.getElementById('received').value||0),payment_method:document.getElementById('method').value,customer_name:document.getElementById('customerName').value,customer_phone:document.getElementById('customerPhone').value};
 const btn=document.getElementById('generate');btn.disabled=true;btn.textContent='Saving bill...';
 try{
  const r=await fetch('/api/bills',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),data=await r.json();
  if(!r.ok)throw new Error(data.error||'Could not save bill');
  const file=await createReceiptImageFile(data.receipt);
  const message=`HI-5 receipt ${data.bill_no} · Grand total ${money(data.receipt.total)}. Intended WhatsApp recipient: +91 8667579407.`;
  if(navigator.share&&navigator.canShare&&navigator.canShare({files:[file]})){
   try{await navigator.share({files:[file],title:data.bill_no,text:message});location.href='/receipt/'+data.bill_id;return}
   catch(error){if(error.name==='AbortError'){location.href='/receipt/'+data.bill_id;return}}
  }
  downloadReceiptFile(file);
  window.location.href=whatsappReceiptUrl(message+' The receipt image was downloaded; attach it to send the full bill.');
 }
 catch(e){alert(e.message);btn.disabled=false;btn.textContent='Generate Bill'}
};
