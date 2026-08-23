from django import forms
from django.core.exceptions import ValidationError

from shared.enums import ContactLifecycle

from .models import Campaign, Contact, ContactNote, TeamMember
from .services.campaigns import ALLOWED_VARIABLES, extract_placeholders, validate_footer
from .services.contacts import clean_tags
from .services.richtext import validate_links, validate_markup
from .services.permissions import can_set_lifecycle, is_lead


class BasecoatMixin:
    """Apply Basecoat's classes to every widget so forms look native."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "select")
            elif isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "input-checkbox")
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("class", "textarea")
            else:
                widget.attrs.setdefault("class", "input")


class CampaignForm(BasecoatMixin, forms.ModelForm):
    #: Entered as a comma-separated list; stored as the JSON list the model wants.
    var_list_raw = forms.CharField(
        required=False,
        label="Variables",
        help_text="Comma-separated, e.g. first_name, company, designation",
    )

    class Meta:
        model = Campaign
        fields = ["title", "mail_sub", "mail_body", "is_html"]
        widgets = {"mail_body": forms.Textarea(attrs={"rows": 14})}
        labels = {"is_html": "Body contains HTML"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["var_list_raw"].initial = ", ".join(self.instance.var_list or [])

    def clean_var_list_raw(self):
        raw = self.cleaned_data["var_list_raw"]
        return [v.strip() for v in raw.split(",") if v.strip()]

    def clean(self):
        """Catch template mistakes here, where they are cheap to fix.

        `{{ compnay }}` discovered at send time means a broken mail to a real
        prospect; discovered here it is a red line under a text box.
        """
        cleaned = super().clean()
        declared = set(cleaned.get("var_list_raw") or [])
        probe = Campaign(
            mail_sub=cleaned.get("mail_sub") or "",
            mail_body=cleaned.get("mail_body") or "",
        )
        used = extract_placeholders(probe)

        unknown = used - ALLOWED_VARIABLES
        if unknown:
            self.add_error(
                "mail_body",
                "Not real Contact fields: " + ", ".join(sorted(unknown))
                + ". Available: " + ", ".join(sorted(ALLOWED_VARIABLES)),
            )

        undeclared = (used - declared) - unknown
        if undeclared:
            self.add_error(
                "var_list_raw",
                "Used in the template but not declared: " + ", ".join(sorted(undeclared)),
            )

        unused = declared - used
        if unused:
            self.add_error(
                "var_list_raw",
                "Declared but never used: " + ", ".join(sorted(unused)),
            )

        # Same bargain as the placeholder checks above: a dead or unsafe link
        # caught here is a red line under a text box; caught at send time it is
        # a broken mail already sitting in a prospect's inbox.
        for problem in validate_links(f"{probe.mail_sub}\n{probe.mail_body}"):
            self.add_error("mail_body", problem)

        # Only when the author asked for raw HTML. Without the checkbox the body
        # is escaped anyway, so `<script>` there is literal text and harmless.
        if cleaned.get("is_html"):
            for problem in validate_markup(cleaned.get("mail_body") or ""):
                self.add_error("mail_body", problem)

        return cleaned

    def save(self, commit=True):
        campaign = super().save(commit=False)
        campaign.var_list = self.cleaned_data["var_list_raw"]
        if commit:
            campaign.save()
        return campaign


class FooterForm(BasecoatMixin, forms.ModelForm):
    """The one thing a sub-campaign owner may edit.

    Not a cut-down CampaignForm: a sub-campaign owner has no business touching
    the subject, the body or the variables, and a form that merely hides those
    fields still round-trips them through a crafted POST.
    """

    class Meta:
        model = Campaign
        fields = ["footer", "footer_is_html"]
        widgets = {"footer": forms.Textarea(attrs={"rows": 6})}
        labels = {"footer_is_html": "Footer contains HTML"}

    def __init__(self, *args, is_lead=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.is_lead = is_lead
        if not is_lead:
            # Removed, not disabled. richtext.py ships without an HTML sanitiser
            # on the explicit grounds that raw HTML is written only by leads --
            # so letting a member set this would quietly invalidate that
            # argument, and a disabled field is not a control.
            self.fields.pop("footer_is_html")

    def clean_footer(self):
        footer = self.cleaned_data.get("footer") or ""
        try:
            validate_footer(footer)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages)
        for problem in validate_links(footer):
            raise forms.ValidationError(problem)
        return footer

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("footer_is_html"):
            for problem in validate_markup(cleaned.get("footer") or ""):
                self.add_error("footer", problem)
        return cleaned


class ContactForm(BasecoatMixin, forms.ModelForm):
    """Add or edit one contact.

    `lifecycle` and `assigned_to` are removed outright for non-leads rather than
    disabled -- a disabled field still round-trips through a crafted POST. The
    service layer strips them again anyway; this is the visible half of the rule.
    """

    #: Same comma-separated convention as CampaignForm.var_list_raw.
    tags_raw = forms.CharField(
        required=False,
        label="Tags",
        help_text="Comma-separated, e.g. fintech, priority, iit-b",
    )

    class Meta:
        model = Contact
        fields = [
            "first_name", "last_name", "email", "phone_no",
            "linkedin", "company", "designation", "lifecycle", "assigned_to",
        ]

    def __init__(self, *args, actor=None, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            self.fields["tags_raw"].initial = ", ".join(self.instance.tags or [])

        if not can_set_lifecycle(actor):
            self.fields.pop("lifecycle", None)
        if not is_lead(actor):
            self.fields.pop("assigned_to", None)
        else:
            self.fields["assigned_to"].queryset = TeamMember.objects.filter(is_active=True)
            self.fields["assigned_to"].required = False

    def clean_tags_raw(self):
        return clean_tags(self.cleaned_data["tags_raw"])

    def clean_email(self):
        """Give the duplicate a name instead of a bare 'already exists'."""
        email = self.cleaned_data["email"].strip().lower()
        clash = Contact.objects.filter(email__iexact=email)
        if self.instance and self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        existing = clash.first()
        if existing:
            owner = existing.assigned_to.name if existing.assigned_to else "nobody"
            raise forms.ValidationError(
                f"{email} is already in the pool as {existing.full_name} "
                f"({existing.company}), assigned to {owner}."
            )
        return email

    def save(self, commit=True):
        contact = super().save(commit=False)
        contact.tags = self.cleaned_data["tags_raw"]
        if commit:
            contact.save()
        return contact


class BulkEditForm(BasecoatMixin, forms.Form):
    """Apply one change to many contacts. Every field blank means 'leave alone'."""

    company = forms.CharField(required=False)
    designation = forms.CharField(required=False)
    tags_add = forms.CharField(required=False, label="Add tags",
                               help_text="Comma-separated")
    tags_remove = forms.CharField(required=False, label="Remove tags",
                                  help_text="Comma-separated")
    lifecycle = forms.ChoiceField(
        required=False, choices=[("", "— leave alone —")] + ContactLifecycle.choices()
    )

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_set_lifecycle(actor):
            self.fields.pop("lifecycle", None)

    def clean_tags_add(self):
        return clean_tags(self.cleaned_data["tags_add"])

    def clean_tags_remove(self):
        return clean_tags(self.cleaned_data["tags_remove"])


class NoteForm(BasecoatMixin, forms.ModelForm):
    class Meta:
        model = ContactNote
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Add a note…"})}


class CsvUploadForm(forms.Form):
    file = forms.FileField(
        label="CSV file",
        help_text="Required columns: first_name, email, company. "
                  "Optional: last_name, phone_no, linkedin, designation, tags "
                  "(semicolon-separated).",
    )


class TokenForm(BasecoatMixin, forms.Form):
    member = forms.ModelChoiceField(queryset=None, label="Team member")
    label = forms.CharField(required=False, label="Label",
                            widget=forms.TextInput(attrs={"placeholder": "e.g. Aarav's MacBook"}))

    def __init__(self, *args, **kwargs):
        members = kwargs.pop("members")
        super().__init__(*args, **kwargs)
        self.fields["member"].queryset = members
