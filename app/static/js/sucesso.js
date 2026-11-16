/* Página de confirmação: copiar o código Pix. A CSP do site proíbe script
   embutido no HTML, por isso este arquivo. */
(() => {
  "use strict";
  document.querySelectorAll("[data-copiar]").forEach((botao) => {
    botao.addEventListener("click", async () => {
      const original = botao.textContent;
      try {
        await navigator.clipboard.writeText(botao.dataset.copiar);
        botao.textContent = "Copiado ✓";
      } catch (_) {
        const campo = document.getElementById("pix-codigo");
        if (campo) { campo.select(); document.execCommand("copy"); }
        botao.textContent = "Copiado ✓";
      }
      setTimeout(() => { botao.textContent = original; }, 1800);
    });
  });
})();
