"""Roles page: create, rename and delete roles (Django Groups) and tick the permissions the app checks.
Anyone with manage_users can look; only Admins and superusers can change anything. Every change is in the Audit Log."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group, Permission
from django.db import transaction
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import reverse

from .account import log_role_change
from .models import CustomUser
from .permissions import (HANDLE_PURCHASING, LOCKED_ROLES, MANAGE_USERS, OVERRIDE_APPROVALS, ROLE_PERMISSION_CODENAMES,
                          ROLE_PERMISSION_LABELS, ROLE_PERMISSION_SECTIONS, action_denied, can_edit_roles,
                          can_grant_group)

ROLE_NAME_MAX = 150  # Group.name max_length
# Taking these off a role you are in could lock you out of this page.
SELF_LOCKOUT_PERMS = (MANAGE_USERS, OVERRIDE_APPROVALS)


def _core_perms():
    """The Roles page permissions, keyed by codename."""
    return {p.codename: p for p in Permission.objects.filter(content_type__app_label='core',
                                                              codename__in=ROLE_PERMISSION_CODENAMES)}


def _may_change_perm(user, codename):
    perm = f'core.{codename}'
    return user.is_superuser or perm == HANDLE_PURCHASING or user.has_perm(perm)


def _name_problem(name, exclude_id=None):
    if not name:
        return "Give the role a name."
    if len(name) > ROLE_NAME_MAX:
        return f"Role names can be at most {ROLE_NAME_MAX} characters."
    if Group.objects.filter(name__iexact=name).exclude(id=exclude_id).exists():
        return f"There is already a role called {name}."
    return None


def _role_url(role_id=None):
    url = reverse('roles')
    return f"{url}?role={role_id}" if role_id else url


def _labels(codenames):
    return ', '.join(ROLE_PERMISSION_LABELS[c] for c in ROLE_PERMISSION_CODENAMES if c in codenames)


@login_required
def roles_view(request):
    if not request.user.has_perm(MANAGE_USERS):
        messages.error(request, "Permission Denied. You don't have permission to manage users.")
        return redirect('dashboard')
    denied = action_denied(request, 'roles')
    if denied:
        messages.error(request, denied)
        return redirect(request.get_full_path())
    if request.method == 'POST':
        return _handle_post(request)

    roles = list(Group.objects.annotate(user_count=Count('user', distinct=True)).order_by('name'))
    selected = None
    role_id = request.GET.get('role', '')
    if role_id.isdigit():
        selected = next((r for r in roles if r.id == int(role_id)), None)
    if selected is None and roles:
        selected = roles[0]

    context = {'roles': roles, 'selected': selected, 'can_edit': can_edit_roles(request.user),
               'name_max': ROLE_NAME_MAX}
    if selected:
        held = set(selected.permissions.filter(content_type__app_label='core', codename__in=ROLE_PERMISSION_CODENAMES)
                   .values_list('codename', flat=True))
        locked = selected.name in LOCKED_ROLES
        editable = context['can_edit'] and not locked and can_grant_group(request.user, selected)
        context.update({
            'locked': locked,
            'editable': editable,
            'sections': [{
                'title': title,
                'perms': [{'code': code, 'label': label, 'checked': code in held,
                           'disabled': not editable or not _may_change_perm(request.user, code)}
                          for code, label in perms],
                'count': sum(1 for code, _ in perms if code in held),
            } for title, perms in ROLE_PERMISSION_SECTIONS],
            'held_count': len(held),
            'other_count': selected.permissions.count() - len(held),
            'members': CustomUser.objects.filter(groups=selected).order_by('username'),
        })
    return render(request, 'roles.html', context)


def _handle_post(request):
    user = request.user
    action = request.POST.get('action')
    role = None
    role_id = request.POST.get('role_id', '')
    if role_id.isdigit():
        role = Group.objects.filter(id=role_id).first()
    if not can_edit_roles(user):
        messages.error(request, "Only an Admin can change roles.")
        return redirect(_role_url(role.id if role else None))

    if action == 'create_role':
        name = request.POST.get('name', '').strip()
        problem = _name_problem(name)
        source = None
        source_id = request.POST.get('copy_from', '')
        if not problem and source_id.isdigit():
            source = Group.objects.filter(id=source_id).first()
            if source and not can_grant_group(user, source):
                problem = f"{source.name} has permissions you don't hold, so you can't copy it."
        if problem:
            messages.error(request, problem)
            return redirect(_role_url())
        with transaction.atomic():
            new_role = Group.objects.create(name=name)
            if source:
                new_role.permissions.set(source.permissions.all())
            log_role_change(request, name, 'role_created',
                            f"Copied from {source.name}" if source else "Started with no permissions")
        messages.success(request, f"Role {name} created." + ("" if source else " Tick its permissions below."))
        return redirect(_role_url(new_role.id))

    if role is None:
        messages.error(request, "That role no longer exists.")
        return redirect(_role_url())
    if role.name in LOCKED_ROLES:
        messages.error(request, f"The {role.name} role always has every permission, so it can't be changed here.")
        return redirect(_role_url(role.id))
    if not can_grant_group(user, role):
        messages.error(request, f"{role.name} has permissions you don't hold, so you can't change it.")
        return redirect(_role_url(role.id))

    if action == 'rename_role':
        name = request.POST.get('name', '').strip()
        if name == role.name:
            return redirect(_role_url(role.id))
        problem = _name_problem(name, exclude_id=role.id)
        if problem:
            messages.error(request, problem)
            return redirect(_role_url(role.id))
        old = role.name
        with transaction.atomic():
            role.name = name
            role.save(update_fields=['name'])
            log_role_change(request, name, 'role_renamed', f"Renamed from {old} to {name}")
        messages.success(request, f"Role {old} renamed to {name}.")
        return redirect(_role_url(role.id))

    if action == 'delete_role':
        members = role.user_set.count()
        if members:
            messages.error(request, f"{role.name} still has {members} user{'s' if members != 1 else ''}. "
                                    "Move them to another role on the Users page first.")
            return redirect(_role_url(role.id))
        name, perm_count = role.name, role.permissions.count()
        with transaction.atomic():
            role.delete()
            log_role_change(request, name, 'role_deleted', f"Had {perm_count} permission{'s' if perm_count != 1 else ''}")
        messages.success(request, f"Role {name} deleted.")
        return redirect(_role_url())

    if action != 'save_permissions':
        messages.error(request, "That action isn't available on this page.")
        return redirect(_role_url(role.id))
    # Only the Roles page permissions change; any other permission the role holds stays.
    perms = _core_perms()
    wanted = set(request.POST.getlist('perms')) & set(perms)
    held = set(role.permissions.filter(content_type__app_label='core', codename__in=perms)
               .values_list('codename', flat=True))
    added, removed = wanted - held, held - wanted
    if not added and not removed:
        messages.info(request, "No changes to save.")
        return redirect(_role_url(role.id))
    refused = [c for c in added | removed if not _may_change_perm(user, c)]
    if refused:
        messages.error(request, f"You can only give or take permissions you hold yourself: {_labels(refused)}.")
        return redirect(_role_url(role.id))
    if not user.is_superuser and user.groups.filter(id=role.id).exists():
        lockout = [c for c in removed if f'core.{c}' in SELF_LOCKOUT_PERMS]
        if lockout:
            messages.error(request, f"You're in {role.name}, so taking off {_labels(lockout)} could lock you out. "
                                    "Ask another Admin to do it.")
            return redirect(_role_url(role.id))
    with transaction.atomic():
        role.permissions.add(*(perms[c] for c in added))
        role.permissions.remove(*(perms[c] for c in removed))
        details = '. '.join(part for part in (
            f"Added: {_labels(added)}" if added else '',
            f"Removed: {_labels(removed)}" if removed else '') if part)
        log_role_change(request, role.name, 'role_permissions_changed', details)
    messages.success(request, f"Permissions for {role.name} saved. They apply on each user's next page load.")
    return redirect(_role_url(role.id))
