CATEGORIES = {
    1: "Notebooks",
    6: "Celulares",
    11: "Televisores",
    14: "Tablets",
    15: "Refrigeradores",
    19: "Lavadoras y Secadoras",
    48: "Wearables",
    780: "Perfumes, Colonias y Fragancias",
}

STORE_IDS = {
    9: "Falabella",
    11: "Paris",
    18: "Ripley",
}

MAX_PAGES_PER_CATEGORY = 3
PAGE_SIZE = 100
REQUEST_DELAY_SECONDS = 1.0

# Categorias donde aplica la deteccion de precio en el percentil mas bajo (sin historial).
# Solo perfumes: en categorias mas homogeneas como notebooks o celulares, el producto
# mas barato no es necesariamente un error de precio, solo genera ruido.
CATEGORY_OUTLIER_IDS = {780}
