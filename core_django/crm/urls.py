from django.urls import path

from . import views

app_name = "crm"

urlpatterns = [
    path("", views.home, name="home"),

    path("contacts/", views.contact_list, name="contact_list"),
    path("contacts/new/", views.contact_new, name="contact_new"),
    path("contacts/import/", views.contact_import, name="contact_import"),
    path("contacts/import/confirm/", views.contact_import_confirm, name="contact_import_confirm"),
    path("contacts/bulk-edit/", views.contact_bulk_edit, name="contact_bulk_edit"),
    path("contacts/<uuid:pk>/", views.contact_detail, name="contact_detail"),
    path("contacts/<uuid:pk>/edit/", views.contact_edit, name="contact_edit"),
    path("contacts/<uuid:pk>/archive/", views.contact_archive, name="contact_archive"),
    path("contacts/<uuid:pk>/delete/", views.contact_delete, name="contact_delete"),

    path("send/", views.send, name="send"),

    path("assign/", views.assign, name="assign"),
    path("assign/apply/", views.assign_apply, name="assign_apply"),

    path("campaigns/", views.campaign_list, name="campaign_list"),
    path("campaigns/new/", views.campaign_edit, name="campaign_new"),
    path("campaigns/<uuid:pk>/", views.campaign_detail, name="campaign_detail"),
    path("campaigns/<uuid:pk>/edit/", views.campaign_edit, name="campaign_edit"),
    path("campaigns/<uuid:pk>/status/", views.campaign_transition, name="campaign_transition"),
    path("campaigns/<uuid:pk>/footer/", views.my_footer, name="my_footer"),

    path("schedules/", views.schedule_list, name="schedule_list"),
    path("schedules/<uuid:pk>/cancel/", views.schedule_cancel, name="schedule_cancel"),
    path("schedules/run/", views.run_queue, name="run_queue"),

    path("settings/gmail/", views.gmail_settings, name="gmail_settings"),
    path("settings/gmail/connect/", views.gmail_connect, name="gmail_connect"),
    path("settings/gmail/disconnect/", views.gmail_disconnect, name="gmail_disconnect"),

    path("teams/", views.team_list, name="team_list"),
    path("teams/<uuid:pk>/", views.team_detail, name="team_detail"),
    path("teams/<uuid:pk>/code/", views.team_rotate_code, name="team_rotate_code"),
    path("teams/<uuid:pk>/role/", views.team_set_role, name="team_set_role"),
    path("teams/<uuid:pk>/distribute/", views.team_distribute, name="team_distribute"),

    path("members/", views.member_list, name="member_list"),
    path("members/<uuid:pk>/sender-name/", views.member_sender_name, name="member_sender_name"),
]
