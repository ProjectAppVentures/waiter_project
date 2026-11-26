"""Corpus y comprobaciones de la receta de caja cerrada v1."""

from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


class ProfileError(Exception):
    pass

SITES = {
    "a1": ("a", "sede-uno", "Sede QA A1"),
    "a2": ("a", "sede-dos", "Sede QA A2"),
    "b1": ("b", "sede-uno", "Sede QA B1"),
}
ACTORS = {
    "dueno": ("a", "owner", None, "Dueño QA"),
    "encargado": ("a", "admin", "a2", "Encargado QA"),
    "cajero": ("a", "cashier", "a1", "Cajero QA"),
    "mesero": ("a", "waiter", "a1", "Mesero QA M-01"),
    "mesero.a2": ("a", "waiter", "a2", "Mesero QA A2"),
    "mesero.b1": ("b", "waiter", "b1", "Mesero QA B1"),
}


def validate_passwords(passwords):
    return (
        isinstance(passwords, dict) and passwords.keys() == ACTORS.keys()
        and all(isinstance(value, str) and 8 <= len(value) <= 128 for value in passwords.values())
    )


def create(config, passwords):
    from django.contrib.auth.hashers import make_password

    from accounts.models import Account
    from catalog.services import seed_organization
    from tables.models import Table
    from tenancy.models import Organization, Restaurant

    organizations = {}
    for key, capacity in (("a", 2), ("b", 1)):
        slug = f"{config['namespace']}-{key}"
        identity = f"waiter/qa/{config['mount_id']}/caja-cerrada/1/{slug}"
        org = Organization.objects.create(
            id=uuid5(NAMESPACE_URL, identity), slug=slug, name=f"Organización QA {key.upper()}",
            status="active", max_restaurants=capacity,
        )
        seed_organization(org)
        organizations[key] = org

    restaurants = {
        key: Restaurant.objects.create(organization=organizations[org], slug=slug, name=name)
        for key, (org, slug, name) in SITES.items()
    }
    for username, (org, role, site, name) in ACTORS.items():
        person = Account.objects.create(
            organization=organizations[org], username=username, role=role, name=name,
            activated=True, password=make_password(passwords[username]),
        )
        if site:
            person.restaurants.add(restaurants[site])

    # La señal de alta de la sede crea el salón base.
    Table.objects.create(floor=restaurants["a1"].floors.get(), number=1)


def business_inventory(config, *, initial):
    from accounts.models import Attendance, LoginPass, Session
    from notifications.models import Notification
    from sales.models import CashMove, CashShift, Course, Order, Payment
    from tenancy.models import PlatformUser

    mail = Path(config["state_dir"]) / "mail"
    if mail.is_symlink() or (mail.exists() and not mail.is_dir()):
        raise ProfileError("El correo del perfil no usa un directorio propio.")
    models = {
        "openings": CashShift, "cash_moves": CashMove, "orders": Order, "payments": Payment,
        "courses": Course, "notifications": Notification, "platform_users": PlatformUser,
    }
    if initial:
        models.update({"sessions": Session, "attendances": Attendance, "login_passes": LoginPass})
    changes = [key for key, model in models.items() if model.objects.exists()]
    if mail.is_dir() and any(mail.iterdir()):
        changes.append("mail")
    if changes:
        raise ProfileError(f"El perfil de caja cerrada contiene actividad en {', '.join(changes)}.")
    return {key: 0 for key in ("openings", "cash_moves", "orders", "payments", "courses", "notifications", "mail")}


def permissions_for(account):
    from accounts.services import restaurants_for
    from sales.policy import ACTIONS, VIEWS, can
    from tenancy.modules import active_modules

    entries = {}
    for restaurant in restaurants_for(account).order_by("pk"):
        account._module_restaurant = restaurant
        entries[str(restaurant.pk)] = {
            "modules": sorted(active_modules(account.organization, restaurant)),
            "views": [key for key in VIEWS if can(account, key)],
            "actions": [key for key in ACTIONS if can(account, key)],
        }
    return entries


def inventory(config, passwords, *, initial=False):
    from django.contrib.auth.hashers import check_password

    from accounts.models import Account
    from tables.models import Table
    from tenancy.models import Organization, Restaurant

    business = business_inventory(config, initial=initial)
    organizations = {
        org.slug: {"id": str(org.pk), "name": org.name, "status": org.status, "plan": org.plan,
                   "role_policy": org.role_policy, "max_restaurants": org.max_restaurants,
                   "timezone": org.timezone}
        for org in Organization.objects.order_by("slug")
    }
    restaurants = [
        {"id": site.pk, "organization_id": str(site.organization_id), "organization": site.organization.slug,
         "slug": site.slug, "name": site.name, "active": site.active, "cash": "cerrada"}
        for site in Restaurant.objects.select_related("organization").order_by("pk")
    ]
    people = list(Account.objects.select_related("organization").order_by("username"))
    if [person.username for person in people] != sorted(ACTORS):
        raise ProfileError("El inventario de actores difiere de la receta.")
    actors = {}
    for person in people:
        if not check_password(passwords[person.username], person.password):
            raise ProfileError("Las credenciales ficticias difieren de la referencia del perfil.")
        actors[person.username] = {
            "id": person.pk, "organization_id": str(person.organization_id), "organization": person.organization.slug,
            "name": person.name, "role": person.role, "active": person.active, "activated": person.activated,
            "schedule": [person.shift_start, person.shift_end],
            "assigned": list(person.restaurants.order_by("pk").values_list("pk", flat=True)),
            "permissions": permissions_for(person),
            "access_ref": {"file": config["credentials_ref"], "section": "accounts", "key": person.username},
        }

    tables = list(Table.objects.select_related("floor__restaurant__organization").order_by("pk")[:2])
    if len(tables) != 1 or not tables[0].token:
        raise ProfileError("El localizador de la mesa QR difiere de la receta.")
    table = tables[0]
    site = table.floor.restaurant
    qr = {
        "restaurant_id": site.pk, "floor_id": table.floor_id, "table_id": table.pk, "table_number": table.number,
        "active": table.active and table.floor.active, "call": table.call,
        "url_template": f"{config['origins']['menu']}/{site.organization.slug}/{site.slug}/t/{{token}}",
        "access_ref": {"model": "tables.Table", "id": table.pk, "field": "token"},
    }
    return {"organizations": organizations, "restaurants": restaurants, "actors": actors,
            "m01": {"actor": "mesero", "restaurant_id": site.pk, "path": "/caja"},
            "qr": qr, "business": business}
