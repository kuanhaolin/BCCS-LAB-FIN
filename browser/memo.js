"use strict"; // Local memo-page behavior only.

document.querySelectorAll(".memo-reveal").forEach((form) => {
  const button = form.querySelector("button");
  const passwordCell = form.closest("tr").querySelector(".memo-password");
  const mask = passwordCell.textContent;

  button.addEventListener("click", async () => {
    if (button.dataset.visible === "true") {
      passwordCell.textContent = mask;
      button.textContent = "顯示";
      button.dataset.visible = "false";
      return;
    }

    button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("reveal failed");
      const data = await response.json();
      if (typeof data.password !== "string") throw new Error("invalid response");
      passwordCell.textContent = data.password;
      button.textContent = "隱藏";
      button.dataset.visible = "true";
    } catch {
      passwordCell.textContent = mask;
      button.textContent = "重試";
    } finally {
      button.disabled = false;
    }
  });
});
