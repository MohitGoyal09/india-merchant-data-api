// Tiny DOM helpers. API text only ever reaches the page through textContent.

const SVG_NS = "http://www.w3.org/2000/svg";

function apply(node, props) {
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.setAttribute("class", value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
}

function append(node, children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  apply(node, props);
  append(node, children);
  return node;
}

export function svg(tag, props, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  apply(node, props);
  append(node, children);
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function $(selector, root = document) {
  const found = root.querySelector(selector);
  if (!found) throw new Error(`missing element: ${selector}`);
  return found;
}

export function setBusy(node, busy) {
  node.setAttribute("aria-busy", busy ? "true" : "false");
}
