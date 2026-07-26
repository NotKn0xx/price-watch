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
