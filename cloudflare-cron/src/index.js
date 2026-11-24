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
    redirect: "error",
    signal: AbortSignal.timeout(10_000),
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
      throw new Error("dispatch no iniciado: falta el secreto GITHUB_TOKEN");
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

    // Solo reintentar una respuesta explicita 5xx. Si se corta la conexion,
    // GitHub pudo haber aceptado el POST; esperamos al proximo cron.
    let respuesta;
    try {
      respuesta = await dispatch(env, cuerpo);
      if (respuesta.status >= 500 && respuesta.status <= 599) {
        await new Promise((r) => setTimeout(r, 2000));
        respuesta = await dispatch(env, cuerpo);
      }
    } catch {
      throw new Error("dispatch sin confirmacion: conexion, timeout o redireccion de GitHub");
    }

    if (respuesta.status !== 204) {
      let pista = "revisar el estado de la API";
      if ([401, 403, 404].includes(respuesta.status)) {
        pista = "revisar acceso, vigencia y permiso Contents: Read and write del token";
      } else if (respuesta.status === 429) {
        pista = "limite de API: se esperara el proximo cron";
      }
      // No incluir el cuerpo de la respuesta ni credenciales en logs.
      throw new Error(`dispatch fallo HTTP ${respuesta.status}: ${pista}`);
    }
    console.log(`dispatch ok · desfase ${desfase}s · cron "${evento.cron}"`);
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
