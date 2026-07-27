# Correo a SoloTodo

**Para:** contacto de SoloTodo (su formulario web o el correo que publiquen)
**Estado:** borrador, lo envía Alvar

---

## Por qué este correo importa más de lo que parece

SoloTodo pasó de ser *una* fuente a ser **la única**: la API de Mercado Libre está cerrada
(403 por política, verificado con token de usuario válido) y no hay alternativa. Todo el
proyecto depende de que ese acceso siga existiendo.

La diferencia con un correo normal de «¿me dejan usar su API?» es que aquí hay algo que
ofrecer: al medir la dispersión de precios entre tiendas aparecieron **fallos concretos de
emparejamiento en su catálogo**. Eso les sirve. Convierte la petición en un intercambio.

**No pedir nada en el primer correo más allá de la conversación.** El objetivo es abrir una
relación, no cerrar un permiso.

---

## Borrador

**Asunto:** Uso de la API pública y un par de casos de emparejamiento que encontré

Hola:

Soy Álvaro, de Chile. Escribo por dos cosas, y la segunda quizás les sirva más que la primera.

Desde hace unos días tengo un proyecto personal que consulta su API pública para seguir la
evolución de precios de algunos productos y avisarme cuando algo cae bastante por debajo de su
propio historial. Es un bot pequeño, con caché agresivo y un retardo entre peticiones; el
código está en github.com/NotKn0xx/price-watch.

Quería preguntarles directamente si ese uso les parece bien y si hay un límite de frecuencia
que prefieran que respete. Prefiero preguntar antes que asumir. Si en algún momento quisiera
publicar algo con esos datos, la atribución a SoloTodo iría visible y con enlace.

Lo segundo: midiendo la dispersión de precios entre tiendas de un mismo producto me aparecieron
algunos casos donde el emparejamiento junta artículos que no son equivalentes. Un par de
ejemplos concretos:

- **Lancôme La Vie est Belle L'Elixir**: la ficha agrupa el *refill* (~$36.990) con el frasco
  completo (~$132.990). Diferencia de 260%.
- **Montblanc Signature**: $29.990 y $64.990 bajo la misma ficha, aparentemente tamaños
  distintos.
- **Cacharel Yes I Am EDP 30 ml**: en una misma tienda aparecen tres publicaciones distintas
  ($29.990, $45.990 y $39.990), y una de las de Paris corresponde a «Yes I Am Rojo», que es
  otra fragancia.

Sobre 40 productos de perfumería que revisé, el 10% supera el 50% de dispersión entre tiendas,
que es donde suelen estar estos casos. Si les sirve, puedo mandarles la lista completa con los
IDs, sin ningún problema y sin pedir nada a cambio.

Gracias por mantener la API abierta. Es bastante raro y se agradece.

Saludos,
Álvaro

---

## Notas para cuando lo envíes

- **No prometas más de lo que vas a hacer.** Si dices que respetarás un límite, respétalo: es
  medible desde su lado.
- **Menciona el repo** solo si sigue público y ordenado. Es tu carta de presentación.
- Si responden pidiendo que bajes la frecuencia, **hazlo el mismo día** y avísales. Esa
  respuesta define la relación.
- Si no responden en dos semanas, no insistas más de una vez. El silencio no es un no.
- **Ofrecer la lista de emparejamientos es sincero, no una moneda de cambio.** Mándala aunque
  no te den nada.
