from django import forms
from django.core.exceptions import ValidationError

from shared.enums import ContactLifecycle

from .models import Campaign, Contact, ContactNote, TeamMember
from .services.campaigns import ALLOWED_VARIABLES, extract_placeholders, validate_footer
from .services.contacts import clean_tags
from .services.richtext import looks_like_html, validate_links, validate_markup
from .services.permissions import assignable_members, can_set_lifecycle, is_lead


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

    **`footer_is_html` is offered to every member, not only to leads.** It was
    lead-only, because richtext.py shipped no sanitiser and the argument ran
    that raw HTML should therefore be written only by leads. What that actually
    produced was a member pasting the signature they use every day -- a table, a
    few spans, a mailto: link -- and mailing it to real prospects with the tags
    showing, because the checkbox that would have rendered it was not on their
    form and nothing said so. The rule blocked working HTML rather than unsafe
    HTML. `richtext.validate_markup` is now a real gate that runs for every
    author regardless of role, which is the control this was standing in for.
    """

    class Meta:
        model = Campaign
        fields = ["footer", "footer_is_html"]
        # Twelve rows, not six: a real signature is a dozen lines of markup,
        # and editing one through a six-line window is most of why "I cannot see
        # what I am doing" was a fair complaint.
        widgets = {"footer": forms.Textarea(attrs={"rows": 12})}
        labels = {"footer_is_html": "Footer contains HTML"}

    def clean_footer(self):
        footer = self.cleaned_data.get("footer") or ""
        try:
            validate_footer(footer)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages)

        # Every problem, not just the first. `raise` inside the loop reported
        # one bad link per save, so a signature with three of them took three
        # round trips to fix -- and each trip looked like a fresh failure.
        problems = validate_links(footer)
        if problems:
            raise forms.ValidationError(problems)
        return footer

    def clean(self):
        cleaned = super().clean()
        footer = cleaned.get("footer") or ""

        if cleaned.get("footer_is_html"):
            for problem in validate_markup(footer):
                self.add_error("footer", problem)

        # The defect itself, caught at the form. Tags with the box unticked go
        # out as visible text, and until now nothing said so -- the mail simply
        # arrived wrong, and the sender found out from a recipient or not at
        # all. Attached to the checkbox rather than the textarea because the
        # checkbox is where the fix is.
        elif looks_like_html(footer):
            self.add_error(
                "footer_is_html",
                "This footer contains HTML tags. Tick this box to render them "
                "— left unticked, they go out as visible text in the mail. If "
                "you meant them literally, remove the angle brackets.",
            )
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
            # Only members of the teams this lead actually leads. The old
            # unfiltered queryset would let a lead of one cohort hand work to
            # another cohort's members.
            self.fields["assigned_to"].queryset = assignable_members(actor)
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


