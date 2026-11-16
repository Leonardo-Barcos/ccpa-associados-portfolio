/* CCPA — abas, máscaras e busca de CEP. Sem dependências externas. */
(() => {
  "use strict";

  /* ------------------------------ Abas ------------------------------ */
  const abas = document.querySelectorAll('.aba[role="tab"]');
  abas.forEach((aba) => {
    aba.addEventListener("click", () => {
      abas.forEach((outra) => {
        const ativa = outra === aba;
        outra.setAttribute("aria-selected", String(ativa));
        const painel = document.getElementById(outra.getAttribute("aria-controls"));
        if (painel) painel.hidden = !ativa;
      });
      history.replaceState(null, "", "?tipo=" + aba.id.replace("tab-", "") + "#formulario");
    });
  });

  /* ---------------------- Mensal / anual ---------------------------- */
  /* Sem JS, os dois ciclos ficam visíveis e o formulário funciona. Aqui a
     página passa a mostrar um de cada vez — e o grupo escondido precisa ter
     os radios DESABILITADOS: um campo required invisível trava o envio com
     um erro do navegador que o visitante nunca chega a ver. */
  document.querySelectorAll(".ciclos").forEach((seletor) => {
    const escopo = seletor.parentElement;
    const grupos = escopo.querySelectorAll(".planos[data-cycle]");
    const botoes = seletor.querySelectorAll(".ciclo");
    if (!grupos.length || !botoes.length) return;

    const mostrar = (ciclo) => {
      grupos.forEach((grupo) => {
        const ativo = grupo.dataset.cycle === ciclo;
        grupo.hidden = !ativo;
        grupo.querySelectorAll("input[type=radio]").forEach((radio) => {
          radio.disabled = !ativo;
          if (!ativo) radio.checked = false;
        });
      });
      botoes.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.cycle === ciclo)));

      const visivel = escopo.querySelector(".planos:not([hidden])");
      if (visivel && !visivel.querySelector("input[type=radio]:checked")) {
        const primeiro = visivel.querySelector("input[type=radio]");
        if (primeiro) primeiro.checked = true;
      }
    };

    /* Se a página voltou com erro, respeita o ciclo que a pessoa tinha escolhido. */
    const marcado = escopo.querySelector("input[type=radio]:checked");
    const inicial = marcado
      ? marcado.closest(".planos").dataset.cycle
      : botoes[0].dataset.cycle;

    seletor.hidden = false;
    botoes.forEach((b) => {
      b.addEventListener("click", () => mostrar(b.dataset.cycle));
    });
    mostrar(inicial);
  });

  /* ---------------------------- Máscaras ---------------------------- */
  const digitos = (v) => v.replace(/\D/g, "");

  const mascaras = {
    cpf: (v) => digitos(v).slice(0, 11)
      .replace(/(\d{3})(\d)/, "$1.$2")
      .replace(/(\d{3})(\d)/, "$1.$2")
      .replace(/(\d{3})(\d{1,2})$/, "$1-$2"),

    /* Desde julho de 2026 o CNPJ pode ter letras nas 12 primeiras posições;
       os 2 dígitos verificadores continuam numéricos. A máscara aceita os
       dois formatos — a antiga apagava as letras enquanto a pessoa digitava. */
    cnpj: (v) => {
      const c = v.toUpperCase().replace(/[^0-9A-Z]/g, "").slice(0, 14);
      const partes = [c.slice(0, 2), c.slice(2, 5), c.slice(5, 8), c.slice(8, 12), c.slice(12, 14)];
      let out = partes[0];
      if (partes[1]) out += "." + partes[1];
      if (partes[2]) out += "." + partes[2];
      if (partes[3]) out += "/" + partes[3];
      if (partes[4]) out += "-" + partes[4];
      return out;
    },

    cep: (v) => digitos(v).slice(0, 8).replace(/^(\d{5})(\d)/, "$1-$2"),

    telefone: (v) => {
      const d = digitos(v).slice(0, 11);
      if (d.length <= 10) {
        return d.replace(/^(\d{2})(\d)/, "($1) $2").replace(/(\d{4})(\d{1,4})$/, "$1-$2");
      }
      return d.replace(/^(\d{2})(\d)/, "($1) $2").replace(/(\d{5})(\d{1,4})$/, "$1-$2");
    },
  };

  const aplicar = (seletor, fn) => {
    document.querySelectorAll(seletor).forEach((el) => {
      el.addEventListener("input", () => { el.value = fn(el.value); });
    });
  };

  aplicar('input[name="cpf"]', mascaras.cpf);
  aplicar('input[name="cnpj"]', mascaras.cnpj);
  aplicar('input[name="postal_code"]', mascaras.cep);
  aplicar('input[name="mobile"], input[name="secondary_phone"]', mascaras.telefone);

  /* ------------------------- Busca de CEP --------------------------- */
  document.querySelectorAll('input[name="postal_code"]').forEach((campoCep) => {
    campoCep.addEventListener("blur", async () => {
      const cep = digitos(campoCep.value);
      if (cep.length !== 8) return;
      const form = campoCep.closest("form");
      try {
        const r = await fetch(`https://viacep.com.br/ws/${cep}/json/`);
        const d = await r.json();
        if (d.erro) return;
        const set = (nome, valor) => {
          const el = form.querySelector(`[name="${nome}"]`);
          if (el && !el.value) el.value = valor || "";
        };
        set("street", d.logradouro);
        set("district", d.bairro);
        set("city", d.localidade);
        const uf = form.querySelector('[name="state"]');
        if (uf && !uf.value) uf.value = d.uf || "";
        const numero = form.querySelector('[name="number"]');
        if (numero && !numero.value) numero.focus();
      } catch (_) { /* offline: o usuário preenche à mão */ }
    });
  });

  /* --------------- Confirmação de senha em tempo real --------------- */
  document.querySelectorAll("form").forEach((form) => {
    const senha = form.querySelector('input[name="password"]');
    const conf = form.querySelector('input[name="password_confirmation"]');
    if (!senha || !conf) return;
    const checar = () => {
      conf.setCustomValidity(
        conf.value && conf.value !== senha.value ? "As senhas não conferem." : ""
      );
    };
    senha.addEventListener("input", checar);
    conf.addEventListener("input", checar);

    form.addEventListener("submit", (ev) => {
      form.querySelectorAll(".erro[data-local]").forEach((p) => p.remove());
      const aviso = form.querySelector(".erro-envio");
      if (aviso) aviso.hidden = true;

      if (!form.checkValidity()) {
        ev.preventDefault();
        /* O balão nativo do navegador some em um segundo e, se o campo já
           está na tela, a página nem rola: a pessoa clica e "nada acontece".
           Então o motivo fica escrito — embaixo do campo e ao lado do botão. */
        const invalidos = [...form.querySelectorAll("input, select, textarea")]
          .filter((el) => !el.disabled && !el.validity.valid);
        invalidos.forEach((el) => {
          el.setAttribute("aria-invalid", "true");
          const msg = document.createElement("p");
          msg.className = "erro";
          msg.dataset.local = "1";
          msg.textContent = el.validationMessage;
          (el.closest(".campo") || el.parentElement).appendChild(msg);
          el.addEventListener("input", () => {
            el.removeAttribute("aria-invalid");
            msg.remove();
          }, { once: true });
        });
        const primeiro = invalidos[0];
        if (aviso && primeiro) {
          const rotulo = form.querySelector(`label[for="${primeiro.id}"]`);
          const nome = rotulo ? rotulo.textContent.replace("*", "").trim() : "um campo";
          aviso.textContent = invalidos.length > 1
            ? `Faltam ajustes em ${invalidos.length} campos, começando por "${nome}".`
            : `Falta ajustar o campo "${nome}".`;
          aviso.hidden = false;
        }
        if (primeiro) {
          primeiro.scrollIntoView({ behavior: "smooth", block: "center" });
          primeiro.focus({ preventScroll: true });
        }
        return;
      }
      const botao = form.querySelector('button[type="submit"]');
      if (botao) { botao.disabled = true; botao.textContent = "Enviando..."; }
    });
  });

  /* Quando a página volta com erro, leva a pessoa ao primeiro campo com
     problema — não ao topo do formulário, que pode estar a uma tela inteira
     de distância do campo destacado. */
  if (document.querySelector(".alerta")) {
    const campo = document.querySelector(
      '.painel:not([hidden]) [aria-invalid="true"]'
    );
    if (campo) {
      campo.scrollIntoView({ behavior: "smooth", block: "center" });
      campo.focus({ preventScroll: true });
    } else {
      document.getElementById("formulario")?.scrollIntoView({ behavior: "smooth" });
    }
  }
})();
