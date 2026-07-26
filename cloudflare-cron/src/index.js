/**
 * Disparador de price-watch.
 *
 * El `schedule` de GitHub Actions es de mejor esfuerzo: medido sobre 3 dias,
 * solo cumplia el 23% de las corridas previstas (14/dia de 60). Para cazar
 * errores de precio, que duran minutos, ese espaciado no sirve.
 *
 * Este Worker llama a repository_dispatch en su lugar, y manda en el payload
 * la hora que el cron pretendia y la que se ejecuto de verdad. Asi el desfase
 * queda registrado en cada corrida y se puede comprobar si el cambio sirvio,
 * en vez de suponerlo.
 */

const REPO = "NotKn0xx/price-watch";
const EVENTO = "escanear";

async function dispatch(env, cuerpo) {
  return fetch(`https://api.github.com/repos/${REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "price-watch-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(cuerpo),
  });
}

export default {
  async scheduled(evento, env) {
    if (!env.GITHUB_TOKEN) {
      console.error("Falta el secreto GITHUB_TOKEN");
      return;
    }

    const programado = evento.scheduledTime;   // lo que el cron pretendia
    const real = Date.now();                   // cuando se ejecuto de verdad
    const desfase = Math.round((real - programado) / 1000);

    const cuerpo = {
      event_type: EVENTO,
      client_payload: {
        programado_iso: new Date(programado).toISOString(),
        real_iso: new Date(real).toISOString(),
        desfase_s: desfase,
        cron: evento.cron,
      },
    };

    // Un reintento: un 5xx puntual de GitHub no deberia costarnos el ciclo.
    let respuesta = await dispatch(env, cuerpo);
    if (respuesta.status >= 500) {
      await new Promise((r) => setTimeout(r, 2000));
      respuesta = await dispatch(env, cuerpo);
    }

    if (respuesta.status === 204) {
      console.log(`dispatch ok · desfase ${desfase}s · cron "${evento.cron}"`);
    } else {
      // El cuerpo del error de GitHub no lleva secretos, es seguro registrarlo.
      console.error(`dispatch fallo ${respuesta.status}: ${await respuesta.text()}`);
    }
  },

  // Solo estado. No dispara nada: una URL publica que lanzara escaneos seria
  // un boton de abuso para cualquiera que la encuentre.
  fetch() {
    return Response.json({
      servicio: "price-watch-cron",
      destino: REPO,
      evento: EVENTO,
      nota: "El escaneo se dispara solo por cron. Este endpoint no hace nada.",
    });
  },
};
