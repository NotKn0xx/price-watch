# price-watch-cron

Worker de Cloudflare que dispara el escaneo cada 10 minutos via
`repository_dispatch`.

## Por que existe

El `schedule` de GitHub Actions es de mejor esfuerzo. Medido sobre 3 dias en
este repo: **14 corridas al dia de las 60 previstas, un 23% de cumplimiento**.
Para errores de precio, que duran minutos, ese espaciado no sirve.

El cron de GitHub sigue configurado en el workflow como red de seguridad: si
este Worker deja de disparar, el bot sigue corriendo mal espaciado en vez de
morir en silencio.

## Como se mide si sirvio

Cada disparo manda en el `client_payload` la hora que el cron pretendia
(`scheduledTime`) y la que se ejecuto de verdad. El workflow escribe ese
desfase en el resumen de la corrida.

Cloudflare tampoco garantiza puntualidad -- su documentacion dice que los
Workers programados corren "en maquinas infrautilizadas". Por eso el desfase
se registra: para comprobar el cambio, no para suponerlo.

Para revisar los desfases acumulados:

```bash
gh run list --repo NotKn0xx/price-watch --workflow price-watch.yml --json databaseId,createdAt,event
```

## Despliegue

```bash
cd cloudflare-cron
npm install
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN
```

El token debe ser **de grano fino**, limitado solo al repositorio
`price-watch`, con permiso **Contents: Read and write**. Un token clasico con
scope `repo` daria acceso a todos tus repositorios para una tarea que solo
necesita uno.

## Ver que esta pasando

```bash
npx wrangler tail          # logs en vivo, incluido el desfase de cada disparo
```

## Recuperacion y apertura de ofertas

1. En Workers & Pages, abre `price-watch-cron` → Settings → Trigger Events →
   View events. Comprueba que existan ambos crons. Si no ves ejecuciones en una
   franja activa, verifica el despliegue antes de tocar el token.
2. En los logs de una invocacion, distingue si falta el secreto, si no hubo
   respuesta confirmada o si GitHub rechazo el despacho. Ante HTTP 401/403/404,
   revisa primero la vigencia del token de grano fino y su acceso a este repo;
   no compartas su valor ni lo subas a Git.
3. La recuperacion se confirma de extremo a extremo: primero aparece
   `dispatch ok` (HTTP 204); luego GitHub Actions muestra una corrida
   `repository_dispatch` con el paso «Puntualidad del disparo» ejecutado.
   El endpoint HTTP de estado **no** prueba esta ruta ni lanza un escaneo.
4. La hora UTC de las 03 se incluye tanto en Worker como en GitHub. Para el
   viernes 27-11-2026, el Worker queda programado desde las 00:00 en Chile
   continental y GitHub desde las 00:02, ambos de mejor esfuerzo. Esto suma
   hasta seis disparos Worker y cuatro de respaldo al dia. Si se necesita
   ejecutar manualmente, el flujo guarda historial y puede avisar por Telegram.

Pruebas locales sin credenciales ni peticiones a GitHub: `npm test`.
