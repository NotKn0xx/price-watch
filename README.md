# price-watch

> Revision del 05-09-2026: [auditoria de arquitectura, precios y seguridad](docs/AUDITORIA-2026-09-05.md).
> Las mediciones historicas de este README no garantizan precision ni cobertura
> actuales. La capa rapida ahora exige una oferta inequivoca, CLP y stock
> confirmado; deja sin alerta los casos ambiguos. Las cifras del backtest miden
> rareza historica, no compras verificadas.

Bot que vigila el catalogo de tiendas chilenas via la API publica de
[Solotodo](https://publicapi.solotodo.com) y avisa por Telegram cuando un
producto aparece a un precio anomalamente bajo respecto de su propia historia.

Corre en GitHub Actions. El historial de precios vive en archivos SQLite
versionados en el repo, asi que no hace falta ninguna base de datos externa.

## Como decide que algo es anomalo

La regla central: **cada producto se compara contra si mismo**, nunca contra
otros productos de la categoria.

1. **Referencia**: mediana de su precio en los ultimos 30 dias, **ponderada por
   tiempo**. Un precio que se sostuvo una semana pesa lo que corresponde; uno
   que duro dos horas, casi nada. Esto es lo que evita que una falla de precio
   arrastre su propia referencia y se auto-silencie.
2. **Madurez**: se exigen al menos 48 horas de historia. Sin eso, la unica
   referencia posible es el "precio normal" que declara la tienda, que viene
   inflado por campanas comerciales, y solo se confia en el si el descuento
   supera el 50%.
3. **Gatillo**: el precio debe caer a `ALERT_MAX_RATIO` o menos de su
   referencia (0.50 por defecto, es decir 50% abajo).
4. **Piso de precio**: bajo `MIN_PRICE_CLP` no se alerta. Una caida del 60% en
   un producto de $3.000 es ruido.

### De donde sale el umbral de 50%

El precio que seguimos es **el mas bajo entre las tiendas filtradas**. Cuando
la tienda mas barata se queda sin stock, ese minimo salta al de la siguiente
tienda, y al reponerse "cae" de vuelta. Ese vaiven no es una oferta.

Midiendo la dispersion de precio entre tiendas para el mismo producto:

| Perfil | mediana | p90 | maximo | productos sobre 50% |
|---|--:|--:|--:|--:|
| Perfumes | 20% | 40% | 45% | 0 |
| Hardware (SSD) | 10% | 21% | 31% | 0 |
| Hardware (monitores) | 5% | 12% | 13% | 0 |
| Celulares (Samsung) | 14% | 33% | 37% | 0 |

Ningun producto, en ningun perfil, tiene mas de 50% de diferencia entre
tiendas. Por eso el umbral por defecto deja todo el ruido de quiebres de stock
por debajo. Hardware usa 0.60 porque su dispersion es mucho menor y porque sus
tiendas no usan el truco del precio normal inflado (0% del catalogo aparece con
descuentos declarados de 20% o mas, contra 9,3% en perfumes).

## Estructura

| Archivo | Rol |
|---|---|
| `solotodo.py` | Cliente de la API: paginacion, sesion con reintentos |
| `detector.py` | Referencia ponderada por tiempo y regla de anomalia |
| `db.py` | Esquema, migracion y acceso a SQLite |
| `run.py` | Orquestacion: escanea, persiste, alerta, resume |
| `notifier.py` | Envio a Telegram |
| `profiles/` | Que categorias y tiendas mirar, y con que umbrales |
| `test_price_watch.py` | Tests de deteccion y almacenamiento |

Y el subsistema de dos capas, que corre en paralelo a `run.py`:

| Archivo | Rol |
|---|---|
| `capa_lenta.py` | `/entities/` + `/pricing_history/`: baseline, percentil, volatilidad |
| `capa_rapida.py` | Consulta la tienda directo, con peticion condicional |
| `extractores.py` / `lectura_producto.py` | Oferta estructurada, moneda, identidad y stock; registro historico de tiendas |
| `http_tienda.py` | Destinos publicos, redirecciones y descargas limitadas |
| `auditar_fichas.py` | Diagnostico de una muestra real sin Telegram ni cambios en SQLite |
| `vigilancia.py` | Que vigilar y cada cuanto |
| `estado_rapido.py` | Sidecar efimero: cabeceras, huellas, contador |
| `run_capas.py` | Orquestacion de ambas capas |
| `backtest.py` | Calibra umbrales sobre la historia ya existente |
| `test_capas.py` | Tests de las dos capas |

## Por que dos capas

Solotodo remuestrea cada ~4h (mediana 4,06h / p90 13,2h, medido). El Worker de
`cloudflare-cron` dispara cada 10 min. O sea que `run.py` consulta 24 veces mas
seguido que lo que su fuente cambia.

La **capa lenta** aporta lo que solo Solotodo tiene: hasta 393 dias de historia
por entidad y cobertura de 142 tiendas. Corre cada ~12h y produce la watchlist.

La **capa rapida** aporta lo unico que Solotodo no puede dar, que es latencia:
consulta la tienda directo cada 10 min sobre esa watchlist acotada.

La diferencia de fondo es que la capa lenta lee `/entities/` y no
`/products/browse/`. Una **entidad** es una tienda y un SKU; un **producto** es
la opinion de Solotodo sobre que SKUs son lo mismo, y esa opinion falla: medido
sobre 52 productos de perfumeria, "Chanel Allure Homme Sport" agrupa 50ml, 100ml
y 150ml con precios de $123.200 a $192.500. Siguiendo el minimo agregado, un
quiebre de stock del de 150ml se ve como una caida del 36% que nunca ocurrio.

## Las dos puertas de la capa rapida

Un ratio bajo contra la mediana no basta. Caso real: AMD Ryzen 5 4500 a $91.900
contra mediana de $159.900 cruza cualquier umbral, pero ese precio aparece en 17
de 61 muestras. Es el ciclo promocional, no un hallazgo.

La segunda puerta exige **rareza**: `precio < p10`, mas barato que el 90% de su
propia historia ponderada por tiempo.

### La ventana de referencia importa mas que el umbral

Con 90 dias hardware saturaba en 57% de precision hicieramos lo que hicieramos.
Con 270:

| ventana | combo | eventos | utiles | ciclos | precision |
|---|---|--:|--:|--:|--:|
| 90d | 0.90 + minimo | 23 | 13 | 10 | 57% |
| 270d | 0.70 + p10 | 23 | 20 | 3 | **87%** |

En 90 dias solo caben 2-3 ciclos, asi que el fondo del ciclo queda **dentro**
del p10. Con 270 entran muchos, el p10 baja por debajo del fondo habitual, y
solo lo excepcional lo cruza.

Se probaron y descartaron otras dos explicaciones: la tendencia a la baja de los
componentes (ventana de 30 dias no mejoro nada) y la corroboracion cruzada
(sobre 70 productos con >=3 tiendas, la caida aislada dio 48% de utiles y la
parcial 39%: no discrimina).

Los umbrales de cada perfil salen de `backtest.py`, que recorre
`pricing_history` con el baseline calculado **solo con datos anteriores** a cada
punto. Sin esa causalidad el backtest se miente solo.

## El historial se guarda comprimido

`price_history` guarda **un tramo por precio constante**, no una fila por
corrida:

```
product_id | price  | first_seen          | last_seen
1234       | 89990  | 2026-07-01 12:00:00 | 2026-07-14 09:15:00
1234       | 74990  | 2026-07-14 09:15:00 | 2026-07-14 09:15:00   <- vigente
```

La duracion de cada tramo se deduce del inicio del siguiente, y la del ultimo
se extiende hasta ahora. **Cuando el precio no cambia no se escribe nada**, asi
que el archivo queda identico byte a byte y no se genera commit.

Esto importa porque el `.db` se commitea en cada corrida: con una fila por
producto por corrida, el repo crecia del orden de 1 MB por hora.

Consecuencia a tener presente: en el tramo vigente `last_seen` queda igual a
`first_seen` **a proposito**. Cualquier consulta que filtre por `last_seen`
dejaria fuera justo los productos mas estables, que son las mejores
referencias. Por eso `load_segments` no filtra por fecha y el recorte a la
ventana lo hace el detector.

## Cadencia y presupuesto

El siguiente presupuesto se calculo cuando el repo era privado. El repositorio
consultado el 05-09-2026 es publico; no usar estas cifras como costo vigente.
En la estimacion original se consideraban jobs redondeados al minuto, con
2.000 minutos/mes en el plan Free. Los tres perfiles corren como tres pasos de
un mismo job (18s de trabajo real) en vez de tres workflows separados.

- Cada 15 min entre las 08:00 y las 22:59 CLT
- Sin escaneo de madrugada: las tiendas no mueven precios ahi

Son 60 jobs/dia = ~1.800 min/mes. No sirve hacer un loop con `sleep` dentro del
job: Actions cobra tiempo de reloj, asi que muchos jobs cortos salen mucho mas
barato que uno largo.

No se baja de 15 minutos porque GitHub atrasa los schedules entre 5 y 20
minutos bajo carga; con intervalo nominal menor la granularidad extra es
ficticia.

## Uso local

```bash
pip install -r requirements.txt
cp .env.example .env        # completa los tokens si quieres probar el envio

# Escaneo real sin enviar nada a Telegram
DRY_RUN=1 PROFILE=perfumes DB_PATH=local.db python run.py

python -m unittest -v      # tests
```

Variables de entorno:

| Variable | Para que |
|---|---|
| `PROFILE` | Que perfil correr (`perfumes`, `hardware`, `celulares`) |
| `DB_PATH` | Archivo SQLite a usar |
| `DRY_RUN` | `1` imprime las alertas en vez de enviarlas |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Credenciales del bot |

## El perfil manda sobre el contenido de su base

Al inicio de cada corrida se descarta todo producto cuya categoria no este en
las `CATEGORIES` del perfil, junto con su historial y sus alertas.

Existe porque paso: al dividir el bot de un perfil unico en tres, el `.db`
original se reutilizo como el de perfumes y arrastro 1.334 productos de
celulares, TV, notebooks y linea blanca. Nunca volvieron a recibir precios
—asi que no generaban alertas— pero eran el 34% de las filas de un archivo que
se commitea en cada cambio.

`prune_history` no los alcanzaba: protege siempre el tramo vigente de cada
producto, y estos tenian exactamente uno. Eran permanentes.

> **Consecuencia:** sacar una categoria de un perfil **borra su historial** en
> la siguiente corrida. Es deliberado —el perfil es la fuente de verdad— pero
> si solo quieres pausarla, comentala en el workflow, no en `CATEGORIES`.
> El `.db` esta versionado, asi que un borrado por error se recupera del
> historial de git.

## Agregar un perfil

Crear `profiles/<nombre>.py` con `CATEGORIES`, `STORE_IDS` y los umbrales, y
agregar un paso al workflow con `PROFILE=<nombre>` y su propio `DB_PATH`.

Antes de fijar `ALERT_MAX_RATIO`, conviene medir la dispersion entre tiendas de
esa categoria y dejar el umbral sobre el maximo observado.

## Detalles de la API que costaron caro

- **El parametro de marca es `db_brands`, no `brands`.** Con `brands` la API
  responde 200 e **ignora el filtro**, devolviendo la categoria completa.
- `/products/browse/` tope `page_size` en 100. Hay que paginar hasta agotar
  `count`, que cuenta *buckets*, no productos.
- `/entities/` tope en 200 filas por pagina y pesa ~4,5 KB por fila por el
  campo `description`. No acepta `fields`/`omit`, asi que no sirve para seguir
  precios por tienda a esta frecuencia.
- `/products/available_entities/` ignora el filtro `ids` cuando se pasan
  muchos: devuelve el catalogo entero. No confiar en el.
- `browse` lista productos sin ninguna entidad disponible (~7% de la muestra).
  Por eso toda alerta se verifica contra `/products/{id}/entities/` antes de
  enviarse, y se descarta si no hay stock comprable.
