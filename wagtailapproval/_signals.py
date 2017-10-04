from numbers import Integral

from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.urlresolvers import reverse
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_save, post_delete

from wagtail.wagtailcore.signals import page_published, page_unpublished
from wagtail.wagtailcore.models import Collection, CollectionMember, Page, GroupCollectionPermission
from wagtail.wagtailimages.models import Image
from wagtail.wagtaildocs.models import Document

from .models import ApprovalPipeline, ApprovalStep, ApprovalTicket
from .signals import step_published, pipeline_published, build_approval_item_list, set_collection_edit, take_ownership
from .approvalitem import ApprovalItem

'''This is a private module for signals that this package uses, not ones provided by this app'''

@receiver(page_published)
def send_published_signals(sender, instance, **kwargs):
    '''This simply watches for a published step or pipeline, and sends a signal
    for it.'''
    if isinstance(instance, ApprovalPipeline):
        pipeline_published.send(sender=type(instance), instance=instance)
    elif isinstance(instance, ApprovalStep):
        step_published.send(sender=type(instance), instance=instance)

@receiver(pipeline_published)
def setup_pipeline_user(sender, instance, **kwargs):
    '''Setup an ApprovalPipeline user'''
    User = get_user_model()

    username_max_length = User._meta.get_field('username').max_length
    username = str(instance)[:username_max_length]
    user = instance.user
    if not user:
        user = User.objects.create(username=username) 
        instance.user = user

    instance.save()

@receiver(step_published)
def setup_group_and_collection(sender, instance, **kwargs):
    '''Create or rename the step's owned groups and collections'''
    pipeline = instance.get_parent().specific
    group_max_length = Group._meta.get_field('name').max_length
    group_name = '{} - {}'.format(pipeline, instance)[:group_max_length]
    group = instance.group
    if not group:
        group = Group.objects.create(name=group_name) 
        access_admin = Permission.objects.get(codename='access_admin')
        group.permissions.add(access_admin)
        instance.group = group

    if group.name != group_name:
        group.name = group_name
        group.save()

    collection_max_length = Collection._meta.get_field('name').max_length
    collection_name = '{} - {}'.format(pipeline, instance)[:collection_max_length]

    collection = instance.collection

    if not collection:
        root_collection = Collection.get_first_root_node()
        collection = root_collection.add_child(name=collection_name) 
        instance.collection = collection

    if collection.name != collection_name:
        collection.name = collection_name
        collection.save()

    # We run a save regardless to ensure permissions are properly set up
    instance.save()

@receiver(post_save)
def catch_collection_objects(sender, instance, created, **kwargs):
    '''If newly-created objects are created inside of a collection that is
    owned by an ApprovalStep, it will automatically take ownership of those
    objects'''
    if created and isinstance(instance, CollectionMember):
        collection = instance.collection

        try:
            step = ApprovalStep.objects.get(collection=collection)
        except ApprovalStep.DoesNotExist:
            return

        step.take_ownership(instance)

@receiver(post_delete)
def approvalticket_cascade_delete(sender, instance, **kwargs):
    '''This deletes objects from :class:`ApprovalTicket` if they are deleted,
    to avoid leaking space (a deleted object would otherwise never be freed
    from the ticket database, as cascades don't work for
    :class:`GenericForeignKey` without a :class:`GenericRelation` ).
    Essentially, this is a custom cascade delete.'''

    # This is to make sure ApprovalTicket objects don't cascade onto
    # themselves, and non-integer pks don't blow up the system
    if isinstance(instance.pk, Integral):
        ApprovalTicket.objects.filter(
            content_type=ContentType.objects.get_for_model(instance),
            object_id=instance.pk).delete()

@receiver(post_delete, sender=ApprovalPipeline)
def delete_owned_user(sender, instance, **kwargs):
    '''This deletes the owned user from :class:`ApprovalPipeline` when the
    pipeline is deleted.'''

    if instance.user:
        instance.user.delete()

@receiver(post_delete, sender=ApprovalStep)
def delete_owned_group_and_collection(sender, instance, **kwargs):
    '''This deletes the owned group and collection from :class:`ApprovalStep`
    when the step is deleted.'''

    if instance.group:
        instance.group.delete()

    if instance.collection:
        instance.collection.delete()

@receiver(post_save, sender=ApprovalStep)
def fix_restrictions(sender, instance, **kwargs):
    '''Update ApprovalStep restrictions on a save.'''

    instance.fix_permissions()

@receiver(build_approval_item_list)
def add_pages(sender, approval_step, **kwargs):
    '''Builds the approval item list for pages'''
    for ticket in ApprovalTicket.objects.filter(
        step=approval_step,
        content_type=ContentType.objects.get_for_model(Page)):
        page = ticket.item
        specific = page.specific
        # Do not allow unpublished pages.  We don't want to end up with a
        # non-live page in a "published" step.
        if page.live:
            yield ApprovalItem(
                title=str(specific),
                view_url=specific.url,
                edit_url=reverse('wagtailadmin_pages:edit', args=(page.id,)),
                delete_url=reverse('wagtailadmin_pages:delete', args=(page.id,)),
                obj=page,
                step=approval_step,
                typename=type(specific).__name__,
                uuid=ticket.pk)

@receiver(build_approval_item_list)
def add_images(sender, approval_step, **kwargs):
    '''Builds the approval item list for images'''
    for ticket in ApprovalTicket.objects.filter(
        step=approval_step,
        content_type=ContentType.objects.get_for_model(Image)):
        image = ticket.item
        yield ApprovalItem(
            title=str(image),
            view_url=image.get_rendition('original').file.url,
            edit_url=reverse('wagtailimages:edit', args=(image.id,)),
            delete_url=reverse('wagtailimages:delete', args=(image.id,)),
            obj=image,
            step=approval_step,
            typename=type(image).__name__,
            uuid=ticket.pk)

@receiver(build_approval_item_list)
def add_document(sender, approval_step, **kwargs):
    '''Builds the approval item list for documents'''
    for ticket in ApprovalTicket.objects.filter(
        step=approval_step,
        content_type=ContentType.objects.get_for_model(Document)):
        document = ticket.item
        yield ApprovalItem(
            title=str(document),
            view_url=document.url,
            edit_url=reverse('wagtaildocs:edit', args=(document.id,)),
            delete_url=reverse('wagtaildocs:delete', args=(document.id,)),
            obj=document,
            step=approval_step,
            typename=type(document).__name__,
            uuid=ticket.pk)

@receiver(set_collection_edit)
def set_image_collection_edit(sender, approval_step, edit **kwargs):
    '''Sets collection permissions for images'''

    collection = approval_step.collection
    group = approval_step.group

    perm = Permission.objects.get(codename='change_image')

    if edit:
        GroupCollectionPermission.objects.get_or_create(group=group, collection=collection, permission=perm)
    else:
        GroupCollectionPermission.objects.filter(group=group, collection=collection, permission=perm).delete()

@receiver(set_collection_edit)
def set_document_collection_edit(sender, approval_step, edit, **kwargs):
    '''Sets collection permissions for documents'''

    collection = approval_step.collection
    group = approval_step.group

    perm = Permission.objects.get(codename='change_document')

    if edit:
        GroupCollectionPermission.objects.get_or_create(group=group, collection=collection, permission=perm)
    else:
        GroupCollectionPermission.objects.filter(group=group, collection=collection, permission=perm).delete()

@receiver(take_ownership)
def take_collection_member(sender, approval_step, object, **kwargs):
    '''Individual take_ownerships for each type should be implemented that also
    take the collection member.  This is a fallback in case something doesn't
    work the way it should'''

    if isinstance(object, CollectionMember):
        if object.collection != approval_step.collection:
            object.collection = approval_step.collection
            object.save()

@receiver(take_ownership)
def update_image_ownership(sender, approval_step, object, **kwargs):
    if isinstance(object, Image):
        if object.collection != approval_step.collection:
            object.collection = approval_step.collection
            object.save()

        if object.uploaded_by_user != pipeline.user
            pipeline = approval_step.get_parent().specific
            object.uploaded_by_user = pipeline.user
            object.save()

@receiver(take_ownership)
def update_document_ownership(sender, approval_step, object, **kwargs):
    if isinstance(object, Document):
        if object.collection != approval_step.collection:
            object.collection = approval_step.collection
            object.save()

        if object.uploaded_by_user != pipeline.user
            pipeline = approval_step.get_parent().specific
            object.uploaded_by_user = pipeline.user
            object.save()
