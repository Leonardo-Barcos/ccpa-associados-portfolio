// Painel de gestão — comportamentos pequenos. A página funciona sem isto;
// aqui só entram conveniências. Arquivo externo porque a CSP do site proíbe
// JavaScript embutido no HTML.
(function () {
  "use strict";

  // Copiar para a área de transferência (link de pagamento, senha provisória).
  document.querySelectorAll("[data-copiar]").forEach(function (button) {
    button.addEventListener("click", function () {
      var original = button.textContent;
      var done = function (text) {
        button.textContent = text;
        setTimeout(function () { button.textContent = original; }, 1800);
      };
      if (!navigator.clipboard) {
        window.prompt("Copie:", button.dataset.copiar);
        return;
      }
      navigator.clipboard.writeText(button.dataset.copiar).then(
        function () { done("Copiado ✓"); },
        function () { window.prompt("Copie:", button.dataset.copiar); }
      );
    });
  });

  // Confirmação antes de ações que não se desfazem sozinhas.
  document.querySelectorAll("form[data-confirmar]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirmar)) event.preventDefault();
    });
  });

  // Botão "ocupado" enquanto o servidor trabalha, para ninguém clicar duas vezes.
  document.querySelectorAll("form[data-ocupado]").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit]");
      if (!button) return;
      button.setAttribute("aria-busy", "true");
      button.textContent = form.dataset.ocupado;
      setTimeout(function () { button.disabled = true; }, 0);
    });
  });
})();
