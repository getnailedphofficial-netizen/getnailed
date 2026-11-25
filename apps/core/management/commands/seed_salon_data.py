from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
import random

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.bookings.models import Appointment, AppointmentService, AppointmentStatusHistory
from apps.customers.models import Customer
from apps.notifications.models import Notification, SalonMessage
from apps.pos.models import CashierShift, Payment, PromoVoucher, ReceiptSequence, Sale, SaleItem
from apps.services.models import Service, ServiceCategory, StaffProfile, StaffSchedule, StaffTimeBlock


class Command(BaseCommand):
    help = (
        "Seeds real salon services, prices, staff technicians, altered Filipino client profiles, "
        "gallery images, and 60 days of realistic appointments & POS sales data."
    )

    def handle(self, *args, **options):
        self._prepare_gallery_images()
        with transaction.atomic():
            owner, cashier, staff_users = self._seed_staff_and_roles()
            customers = self._seed_customers()
            services_by_name = self._seed_services_and_categories(staff_users)
            self._seed_vouchers()
            total_sales, total_appts = self._seed_60_days_data(
                owner=owner,
                cashier=cashier,
                staff_users=staff_users,
                customers=customers,
                services_by_name=services_by_name,
            )
            self._seed_messages_and_notifications(owner, staff_users, customers)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(staff_users)} nail technicians, {len(customers)} Filipino client profiles, "
                f"{len(services_by_name)} services, {total_appts} appointments, and {total_sales} POS sales across 60 days."
            )
        )
        call_command("ensure_ml_forecast")

    def _prepare_gallery_images(self):
        try:
            from PIL import Image, ImageEnhance
        except ImportError:
            return

        base_dir = Path(settings.BASE_DIR)
        gallery_dir = base_dir / "static" / "images" / "gallery"
        gallery_dir.mkdir(parents=True, exist_ok=True)

        collage_path = base_dir / "static" / "images" / "nails-gallery.png"
        if collage_path.exists():
            im = Image.open(collage_path).convert("RGB")
            # Crop the 4 distinct nail photos from nails-gallery.png (1248x768)
            crops = {
                "nail-1.jpg": (8, 8, 512, 340),          # Starlight Navy Cat Eye
                "nail-2.jpg": (646, 10, 1002, 362),      # Champagne Cat Eye Soft Gel
                "nail-3.jpg": (102, 388, 576, 760),      # White 3D Floral & Gold Leaf
                "nail-4.jpg": (602, 398, 1016, 730),     # Burgundy Marble & Gold Foil
                "get-nailed-hero.jpg": (602, 398, 1016, 730),
            }
            for filename, box in crops.items():
                cropped = im.crop(box).resize((800, 640), Image.Resampling.LANCZOS)
                cropped = ImageEnhance.Sharpness(cropped).enhance(1.12)
                cropped.save(gallery_dir / filename, quality=92)

        service_imgs = {
            "service-gel-polish.png": "nail-5.jpg",
            "service-manicure.png": "nail-6.jpg",
            "service-nail-art.png": "nail-7.jpg",
        }
        for src_name, dst_name in service_imgs.items():
            src_path = base_dir / "static" / "images" / "services" / src_name
            if src_path.exists():
                s_im = Image.open(src_path).convert("RGB").resize((800, 640), Image.Resampling.LANCZOS)
                s_im.save(gallery_dir / dst_name, quality=92)

        # Also save a clean cropped version of the full 4-grid without the right white margin as nail-8.jpg
        if collage_path.exists():
            im = Image.open(collage_path).convert("RGB")
            full_grid = im.crop((0, 0, 1020, 768)).resize((960, 720), Image.Resampling.LANCZOS)
            full_grid.save(gallery_dir / "nail-8.jpg", quality=92)

    def _seed_staff_and_roles(self):
        default_pw = "GetNailed2026!"
        now = timezone.now()

        # 1. Owner: Trina Ocampo (from Business Card)
        owner, created = User.objects.get_or_create(
            email="trina.ocampo@getnailed.ph",
            defaults={
                "first_name": "Trina",
                "last_name": "Ocampo",
                "phone_number": "09050843892",
                "role": User.Role.OWNER,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
                "is_active_staff_member": True,
                "email_verified_at": now,
            },
        )
        owner.first_name = "Trina"
        owner.last_name = "Ocampo"
        owner.phone_number = "09050843892"
        owner.role = User.Role.OWNER
        owner.is_staff = True
        owner.is_superuser = True
        owner.is_active = True
        owner.is_active_staff_member = True
        owner.email_verified_at = owner.email_verified_at or now
        owner.set_password(default_pw)
        owner.save()

        # Ensure owner@getnailed.com also works seamlessly
        alt_owner, _ = User.objects.get_or_create(
            email="owner@getnailed.com",
            defaults={
                "first_name": "Trina",
                "last_name": "Ocampo",
                "phone_number": "09050843892",
                "role": User.Role.OWNER,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
                "is_active_staff_member": True,
                "email_verified_at": now,
            },
        )
        alt_owner.first_name = "Trina"
        alt_owner.last_name = "Ocampo"
        alt_owner.role = User.Role.OWNER
        alt_owner.is_staff = True
        alt_owner.is_superuser = True
        alt_owner.is_active = True
        alt_owner.is_active_staff_member = True
        alt_owner.email_verified_at = alt_owner.email_verified_at or now
        alt_owner.set_password(default_pw)
        alt_owner.save()

        # 2. Cashier / Receiver: Phen (from Spreadsheet "RECEIVED: Phen")
        cashier, _ = User.objects.get_or_create(
            email="phen@getnailed.ph",
            defaults={
                "first_name": "Phen",
                "last_name": "Santos",
                "phone_number": "09178452310",
                "role": User.Role.CASHIER,
                "is_active": True,
                "is_active_staff_member": True,
                "can_use_pos": True,
                "can_manage_bookings": True,
                "can_manage_customers": True,
                "can_assign_services": True,
                "email_verified_at": now,
            },
        )
        cashier.first_name = "Phen"
        cashier.last_name = "Santos"
        cashier.role = User.Role.CASHIER
        cashier.is_active = True
        cashier.is_active_staff_member = True
        cashier.can_use_pos = True
        cashier.can_manage_bookings = True
        cashier.can_manage_customers = True
        cashier.can_assign_services = True
        cashier.email_verified_at = cashier.email_verified_at or now
        cashier.set_password(default_pw)
        cashier.save()

        alt_cashier, _ = User.objects.get_or_create(
            email="cashier@getnailed.com",
            defaults={
                "first_name": "Phen",
                "last_name": "Santos",
                "phone_number": "09178452310",
                "role": User.Role.CASHIER,
                "is_active": True,
                "is_active_staff_member": True,
                "can_use_pos": True,
                "can_manage_bookings": True,
                "can_manage_customers": True,
                "can_assign_services": True,
                "email_verified_at": now,
            },
        )
        alt_cashier.role = User.Role.CASHIER
        alt_cashier.is_active = True
        alt_cashier.is_active_staff_member = True
        alt_cashier.can_use_pos = True
        alt_cashier.can_manage_bookings = True
        alt_cashier.can_manage_customers = True
        alt_cashier.can_assign_services = True
        alt_cashier.email_verified_at = alt_cashier.email_verified_at or now
        alt_cashier.set_password(default_pw)
        alt_cashier.save()

        # 3. Real Nail Technicians from the Spreadsheet (NAIL TECH column)
        tech_specs = [
            ("Rita", "Villanueva", "rita@getnailed.ph", "09172019384", "Gel Manicure, Pedicure & Foot Spa Packages", True),
            ("Nory", "Macapagal", "nory@getnailed.ph", "09183920481", "Softgel Extensions, Cat Eye & Nail Art", True),
            ("Jessa", "Manansala", "jessa@getnailed.ph", "09954829103", "Classic Manicure, Pedicure & Foot Reflex", True),
            ("Verna", "Pangilinan", "verna@getnailed.ph", "09175930284", "Gel Polish, Foot Spa & Paraffin Treatments", False),
            ("Sheng", "Dimaculangan", "sheng@getnailed.ph", "09274839201", "Eyelash Extensions, Lash Lift & Eyebrow Threading", False),
            ("Manilyn", "Tolentino", "manilyn@getnailed.ph", "09158392049", "Deluxe Spa Pedicure, Foot Massage & Waxing", False),
            ("Aila", "Capistrano", "aila@getnailed.ph", "09176049382", "Softgel Extensions, Chrome & Custom Nail Art", False),
            ("Ann", "Sarmiento", "ann@getnailed.ph", "09194839205", "Gel Manicure, Cat Eye & Ear Candling", False),
            ("Stef", "Evangelista", "stef@getnailed.ph", "09179403821", "Russian Manicure, Gel Removal & Spa Packages", False),
        ]

        staff_users = {}
        for first, last, email, phone, specialty, grant_ops in tech_specs:
            u, _ = User.objects.get_or_create(
                email=email,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "phone_number": phone,
                    "role": User.Role.STAFF,
                    "is_active": True,
                    "is_active_staff_member": True,
                    "can_use_pos": grant_ops,
                    "can_manage_bookings": grant_ops,
                    "can_manage_customers": grant_ops,
                    "can_assign_services": grant_ops,
                    "email_verified_at": now,
                },
            )
            u.first_name = first
            u.last_name = last
            u.phone_number = phone
            u.role = User.Role.STAFF
            u.is_active = True
            u.is_active_staff_member = True
            u.can_use_pos = grant_ops
            u.can_manage_bookings = grant_ops
            u.can_manage_customers = grant_ops
            u.can_assign_services = grant_ops
            u.email_verified_at = u.email_verified_at or now
            u.set_password(default_pw)
            u.save()

            profile, _ = StaffProfile.objects.get_or_create(
                user=u,
                defaults={
                    "specialty": specialty,
                    "availability_status": StaffProfile.Availability.AVAILABLE,
                    "is_active": True,
                },
            )
            profile.specialty = specialty
            profile.availability_status = StaffProfile.Availability.AVAILABLE
            profile.is_active = True
            profile.save()

            for day_idx in range(7):
                StaffSchedule.objects.update_or_create(
                    staff=u,
                    day_of_week=day_idx,
                    defaults={
                        "start_time": time(9, 0),
                        "end_time": time(20, 0),
                        "lunch_start": time(12, 0),
                        "lunch_end": time(13, 0),
                        "is_working": True,
                    },
                )
            staff_users[first] = u

        # Also ensure staff@getnailed.com is an active technician profile
        demo_staff, _ = User.objects.get_or_create(
            email="staff@getnailed.com",
            defaults={
                "first_name": "Rita",
                "last_name": "Villanueva (Tech)",
                "phone_number": "09172019384",
                "role": User.Role.STAFF,
                "is_active": True,
                "is_active_staff_member": True,
                "can_use_pos": True,
                "can_manage_bookings": True,
                "can_manage_customers": True,
                "can_assign_services": True,
                "email_verified_at": now,
            },
        )
        demo_staff.role = User.Role.STAFF
        demo_staff.is_active = True
        demo_staff.is_active_staff_member = True
        demo_staff.can_use_pos = True
        demo_staff.can_manage_bookings = True
        demo_staff.can_manage_customers = True
        demo_staff.can_assign_services = True
        demo_staff.email_verified_at = demo_staff.email_verified_at or now
        demo_staff.set_password(default_pw)
        demo_staff.save()
        StaffProfile.objects.update_or_create(
            user=demo_staff,
            defaults={
                "specialty": "Senior Nail Artist & Spa Specialist",
                "availability_status": StaffProfile.Availability.AVAILABLE,
                "is_active": True,
            },
        )

        # Sample Time Blocks for tomorrow & 2 days ahead so Time Blocks page has realistic entries
        today = timezone.localdate()
        StaffTimeBlock.objects.get_or_create(
            staff=staff_users["Nory"],
            date=today + timedelta(days=1),
            start_time=time(14, 0),
            end_time=time(15, 30),
            defaults={"reason": "Bridal nail art prep & supply inventory"},
        )
        StaffTimeBlock.objects.get_or_create(
            staff=staff_users["Sheng"],
            date=today + timedelta(days=2),
            start_time=time(10, 0),
            end_time=time(11, 30),
            defaults={"reason": "Lash extension restock & sterilization"},
        )

        return owner, cashier, staff_users

    def _seed_customers(self):
        default_pw = "GetNailed2026!"
        now = timezone.now()

        # Authentic, non-generic Filipino names (altered from raw spreadsheet names)
        filipino_clients = [
            ("Camille", "Villafuerte", "camille.villafuerte@gmail.com", "09173048291", True),
            ("Clarisse", "Dimaculangan", "clarisse.dimaculangan@gmail.com", "09184920174", True),
            ("Therese", "Macapagal", "therese.macapagal@gmail.com", "09958371625", True),
            ("Katrina", "Magbanua", "katrina.magbanua@gmail.com", "09176294810", True),
            ("Alyssa", "Panganiban", "alyssa.panganiban@gmail.com", "09274819203", True),
            ("Denise", "Catacutan", "denise.catacutan@gmail.com", "09159384721", True),
            ("Patricia", "Laxamana", "patricia.laxamana@gmail.com", "09175029384", True),
            ("Bianca", "Marasigan", "bianca.marasigan@gmail.com", "09193847561", True),
            ("Mariel", "Evangelista", "mariel.evangelista@gmail.com", "09178392045", True),
            ("Cheska", "Tolentino", "cheska.tolentino@gmail.com", "09284930218", True),
            ("Danica", "Samonte", "danica.samonte@gmail.com", "09174920381", True),
            ("Angelica", "Vergara", "angelica.vergara@gmail.com", "09185039482", True),
            ("Nadine", "Policarpio", "nadine.policarpio@gmail.com", "09952938471", False),
            ("Valerie", "Sarmiento", "valerie.sarmiento@gmail.com", "09178493021", False),
            ("Kristine", "Manansala", "kristine.manansala@gmail.com", "09275948302", False),
            ("Mikaela", "Solis", "mikaela.solis@gmail.com", "09154938201", False),
            ("Rochelle", "Galang", "rochelle.galang@gmail.com", "09173948502", False),
            ("Charmaine", "Pangilinan", "charmaine.pangilinan@gmail.com", "09192837465", False),
            ("Eloisa", "Quiambao", "eloisa.quiambao@gmail.com", "09176859403", False),
            ("Stephanie", "Ilagan", "stephanie.ilagan@gmail.com", "09283746591", False),
            ("Glaiza", "Capistrano", "glaiza.capistrano@gmail.com", "09175849302", False),
            ("Trisha", "Malabanan", "trisha.malabanan@gmail.com", "09954837261", False),
            ("Janelle", "Henson", "janelle.henson@gmail.com", "09174839204", False),
            ("Beauregard", "Pamintuan", "beauregard.pamintuan@gmail.com", "09183948571", False),
            ("Lorenzo", "Soriano", "lorenzo.soriano@gmail.com", "09179384756", False),
            ("Cedric", "Tuazon", "cedric.tuazon@gmail.com", "09273849501", False),
            ("Jericho", "Dimaano", "jericho.dimaano@gmail.com", "09158493027", False),
            ("Rafael", "Lacanienta", "rafael.lacanienta@gmail.com", "09174059382", False),
            ("Vince", "Gatdula", "vince.gatdula@gmail.com", "09194857362", False),
            ("Maricar", "Balingit", "maricar.balingit@gmail.com", "09173849205", False),
            ("Shaira", "Pineda", "shaira.pineda@gmail.com", "09284756392", False),
            ("Rizza", "Guevarra", "rizza.guevarra@gmail.com", "09175948372", False),
            ("Hannah", "Sicat", "hannah.sicat@gmail.com", "09953847562", False),
            ("Monica", "Mallari", "monica.mallari@gmail.com", "09178493056", False),
            ("Eunice", "Turla", "eunice.turla@gmail.com", "09184758392", False),
            ("Pauline", "Nucum", "pauline.nucum@gmail.com", "09173948271", False),
            ("Ysabel", "Bondoc", "ysabel.bondoc@gmail.com", "09274859304", False),
            ("Liezel", "Aguas", "liezel.aguas@gmail.com", "09153849206", False),
            ("Katrina", "Viray", "katrina.viray@gmail.com", "09176948302", False),
            ("Czarina", "Dungca", "czarina.dungca@gmail.com", "09193847502", False),
            ("Elaine", "Tayag", "elaine.tayag@gmail.com", "09175849201", False),
            ("Bernice", "Halili", "bernice.halili@gmail.com", "09283948502", False),
            ("Maxine", "Cunanan", "maxine.cunanan@gmail.com", "09174859301", False),
            ("Justine", "Dayrit", "justine.dayrit@gmail.com", "09954839201", False),
            ("Regine", "Salonga", "regine.salonga@gmail.com", "09178394021", False),
        ]

        customers = []
        for first, last, email, phone, create_login in filipino_clients:
            user_obj = None
            if create_login:
                user_obj, _ = User.objects.get_or_create(
                    email=email,
                    defaults={
                        "first_name": first,
                        "last_name": last,
                        "phone_number": phone,
                        "role": User.Role.CUSTOMER,
                        "is_active": True,
                        "is_active_staff_member": False,
                        "email_verified_at": now,
                    },
                )
                user_obj.first_name = first
                user_obj.last_name = last
                user_obj.phone_number = phone
                user_obj.role = User.Role.CUSTOMER
                user_obj.is_active = True
                user_obj.email_verified_at = user_obj.email_verified_at or now
                user_obj.set_password(default_pw)
                user_obj.save()

            cust, _ = Customer.objects.update_or_create(
                email=email,
                defaults={
                    "user": user_obj,
                    "first_name": first,
                    "last_name": last,
                    "phone": phone,
                    "notes": "Preferred branch: Plaridel (Rocka Commercial Complex) / Retiro QC",
                },
            )
            customers.append(cust)

        return customers

    def _seed_services_and_categories(self, staff_users):
        # Exact Categories & Services from the uploaded Get Nailed Daily Sales Log images
        catalog = {
            "Classic Nail Care": (
                "Signature manicures, pedicures, and kiddie nail care using hygienic tools.",
                [
                    ("Manicure", Decimal("140.00"), 30, "Classic nail shaping, cuticle care, hand massage, and polish."),
                    ("Pedicure", Decimal("180.00"), 35, "Classic toenail shaping, cuticle cleaning, and regular polish."),
                    ("Manicure / Pedicure", Decimal("320.00"), 60, "Complete classic manicure and pedicure combo."),
                    ("Deluxe Manicure / Pedicure", Decimal("345.00"), 60, "Classic manicure and pedicure with deep cleaning upgrade."),
                    ("Kiddie Pedicure", Decimal("100.00"), 25, "Gentle toenail trim, shaping, and polish for kids."),
                    ("Kiddie Manicure / Pedicure / Foot Spa", Decimal("350.00"), 50, "Complete pampering nail & foot spa package for kids."),
                ],
            ),
            "Gel Polish & Softgel Extensions": (
                "Long-lasting UV gel polish, magnetic cat eye, custom nail art, and softgel extensions.",
                [
                    ("Gel Manicure", Decimal("450.00"), 45, "Chip-free, high-gloss gel manicure with Russian-style cuticle prep."),
                    ("Gel Pedicure", Decimal("550.00"), 50, "Long-wearing gel polish pedicure with full cuticle care."),
                    ("Gel Manicure / Pedicure", Decimal("625.00"), 75, "Signature gel manicure and pedicure package."),
                    ("Gel Manicure / Cat Eye", Decimal("650.00"), 60, "Gel manicure finished with magnetic velvet or galaxy cat-eye effect."),
                    ("Softgel Ext.", Decimal("1200.00"), 90, "Full-cover sculpted softgel nail extensions with gel color."),
                    ("Softgel Ext. / Cat Eye", Decimal("1350.00"), 100, "Softgel nail extensions with magnetic cat-eye finish."),
                    ("Softgel Ext. / Pedicure / Foot Spa / Cat Eye", Decimal("1710.00"), 120, "Complete softgel cat-eye extensions paired with pedicure and foot spa."),
                    ("Softgel Ext. / Gel Pedicure / Foot Spa", Decimal("2000.00"), 130, "Luxury softgel extensions, gel pedicure, and relaxing foot spa package."),
                    ("Gel Removal / Cleaning", Decimal("150.00"), 25, "Safe soak-off gel or softgel extension removal and nail conditioning."),
                    ("Custom Nail Art / Chrome Add-on", Decimal("185.00"), 25, "Bespoke 3D floral, gold foil, French tip, or mirror chrome nail art."),
                ],
            ),
            "Foot Spa, Massage & Packages": (
                "Relaxing foot spa scrubs, reflexology, paraffin therapy, and full pampering combos.",
                [
                    ("Foot Spa", Decimal("300.00"), 40, "Exfoliating foot soak, callus smoothing scrub, and hydrating mask."),
                    ("Pedicure / Foot Spa", Decimal("400.00"), 60, "Classic pedicure paired with our signature soothing foot spa."),
                    ("Deluxe Pedicure / Foot Spa", Decimal("460.00"), 65, "Upgraded pedicure and whitening/menthol foot spa treatment."),
                    ("Manicure / Pedicure / Foot Spa", Decimal("470.00"), 75, "Our bestselling trio: classic manicure, pedicure, and foot spa."),
                    ("Foot Spa / Foot Massage", Decimal("500.00"), 60, "Rejuvenating foot spa followed by tension-relieving foot massage."),
                    ("Manicure / Pedicure / Foot Spa / Foot Reflex", Decimal("750.00"), 90, "Full manicure, pedicure, foot spa, and pressure-point foot reflexology."),
                    ("Manicure / Pedicure / Foot Spa / Foot Massage", Decimal("770.00"), 90, "Complete manicure, pedicure, foot spa, and extended foot massage."),
                    ("Manicure / Gel Pedicure / Foot Spa", Decimal("850.00"), 90, "Classic manicure, long-wear gel pedicure, and soothing foot spa."),
                    ("Gel Manicure / Pedicure / Foot Massage", Decimal("860.00"), 90, "Gel manicure, classic pedicure, and relaxing foot massage."),
                    ("Gel Manicure / Pedicure / Gel Removal / Cat Eye", Decimal("870.00"), 95, "Gel removal plus fresh cat-eye gel manicure and pedicure."),
                    ("Gel Manicure / Stone / Foot Reflex", Decimal("900.00"), 90, "Gel manicure with embellishments and therapeutic foot reflexology."),
                    ("Gel Manicure / Pedicure / Nail Art / Foot Paraffin", Decimal("1004.00"), 105, "Gel mani-pedi with custom nail art and warm soothing foot paraffin."),
                    ("Gel Manicure / Pedicure / Foot Spa / Gel Removal / Cat Eye", Decimal("1070.00"), 110, "Ultimate gel refresh package with foot spa, removal, and cat eye."),
                ],
            ),
            "Lashes, Brows, Waxing & Spa": (
                "Eyebrow threading, eyelash lifts and extensions, gentle waxing, and ear candling.",
                [
                    ("Eyebrow Threading", Decimal("150.00"), 20, "Precision eyebrow shaping and clean-up using gentle cotton thread."),
                    ("Upper Lip / Eyebrow Threading", Decimal("300.00"), 30, "Combined upper lip and eyebrow threading package."),
                    ("Ear Candling", Decimal("250.00"), 30, "Relaxing holistic ear candling wellness treatment."),
                    ("Under Arm Wax", Decimal("300.00"), 25, "Smooth, gentle underarm hair removal waxing."),
                    ("Half Leg Wax", Decimal("450.00"), 35, "Lower leg warm waxing treatment for silky-smooth skin."),
                    ("Eyelash Lift", Decimal("500.00"), 45, "Natural lash perm and keratin lift for curled, wide-awake lashes."),
                    ("Eyebrow Threading / Eyelash Lift", Decimal("650.00"), 60, "Defined brows and lifted lashes in one convenient session."),
                    ("Full Leg Wax", Decimal("750.00"), 50, "Complete full-leg waxing with soothing post-wax lotion."),
                    ("Eyelash Ext.", Decimal("1150.00"), 75, "Full, lightweight classic or hybrid individual eyelash extensions."),
                ],
            ),
        }

        active_names = set()
        services_by_name = {}

        for cat_name, (cat_desc, items) in catalog.items():
            category, _ = ServiceCategory.objects.update_or_create(
                name=cat_name,
                defaults={"description": cat_desc},
            )
            for s_name, price, duration, s_desc in items:
                svc, _ = Service.objects.update_or_create(
                    category=category,
                    name=s_name,
                    defaults={
                        "price": price,
                        "duration_minutes": duration,
                        "description": s_desc,
                        "is_active": True,
                    },
                )
                active_names.add(svc.pk)
                services_by_name[s_name] = svc

        # Deactivate any old leftover services not in the official salon menu
        Service.objects.exclude(pk__in=active_names).update(is_active=False)

        # Ensure all technicians can perform all active services
        all_active_services = list(services_by_name.values())
        for staff_user in staff_users.values():
            if hasattr(staff_user, "staff_profile"):
                staff_user.staff_profile.skills.set(all_active_services)

        return services_by_name

    def _seed_vouchers(self):
        today = timezone.localdate()
        vouchers = [
            ("GETNAILED10", "10% Off Approved Online Booking Promo", PromoVoucher.DiscountType.PERCENT, Decimal("10.00"), Decimal("300.00")),
            ("LOYALTY5", "5% Off Loyalty Card 3rd Visit Reward", PromoVoucher.DiscountType.PERCENT, Decimal("5.00"), Decimal("250.00")),
            ("LOYALTY10", "10% Off Loyalty Card 6th Visit Reward", PromoVoucher.DiscountType.PERCENT, Decimal("10.00"), Decimal("300.00")),
            ("LOYALTY20", "20% Off Loyalty Card 12th Visit Reward", PromoVoucher.DiscountType.PERCENT, Decimal("20.00"), Decimal("400.00")),
            ("BIRTHMONTH20", "20% Off On Your Birthmonth Loyalty Treat", PromoVoucher.DiscountType.PERCENT, Decimal("20.00"), Decimal("400.00")),
            ("FREEEARCANDLE", "Free Ear Candling (9th Visit Loyalty Card)", PromoVoucher.DiscountType.FIXED, Decimal("250.00"), Decimal("250.00")),
        ]
        for code, desc, dtype, val, min_spend in vouchers:
            PromoVoucher.objects.update_or_create(
                code=code,
                defaults={
                    "description": desc,
                    "discount_type": dtype,
                    "discount_value": val,
                    "min_spend": min_spend,
                    "valid_from": today - timedelta(days=90),
                    "valid_until": today + timedelta(days=365),
                    "is_active": True,
                },
            )

    def _seed_60_days_data(self, owner, cashier, staff_users, customers, services_by_name):
        # Clean up previous synthetic sales & appointments so the 60-day history is clean and exact
        Sale.objects.filter(receipt_number__startswith="AUTO-ML-").delete()
        Sale.objects.filter(receipt_number__startswith="GN-SEED-").delete()
        Appointment.objects.filter(notes__startswith="[SEED-60D]").delete()

        rng = random.Random(20261028)
        today = timezone.localdate()
        tech_list = list(staff_users.values())
        receivers = [cashier, owner]

        # Weighted service baskets matching the actual Google Sheets rows
        weekday_services = [
            services_by_name["Manicure / Pedicure"],
            services_by_name["Deluxe Manicure / Pedicure"],
            services_by_name["Pedicure / Foot Spa"],
            services_by_name["Deluxe Pedicure / Foot Spa"],
            services_by_name["Manicure / Pedicure / Foot Spa"],
            services_by_name["Gel Manicure"],
            services_by_name["Gel Pedicure"],
            services_by_name["Gel Manicure / Pedicure"],
            services_by_name["Manicure"],
            services_by_name["Pedicure"],
            services_by_name["Foot Spa"],
            services_by_name["Foot Spa / Foot Massage"],
            services_by_name["Eyebrow Threading"],
            services_by_name["Upper Lip / Eyebrow Threading"],
            services_by_name["Eyelash Lift"],
            services_by_name["Half Leg Wax"],
            services_by_name["Full Leg Wax"],
            services_by_name["Ear Candling"],
            services_by_name["Kiddie Pedicure"],
        ]

        # Friday-Sunday prioritizes higher-value packages (>300) as per the salon's weekend policy
        weekend_packages = [
            services_by_name["Manicure / Pedicure / Foot Spa"],
            services_by_name["Pedicure / Foot Spa"],
            services_by_name["Deluxe Pedicure / Foot Spa"],
            services_by_name["Gel Manicure / Pedicure"],
            services_by_name["Gel Manicure / Cat Eye"],
            services_by_name["Softgel Ext."],
            services_by_name["Softgel Ext. / Cat Eye"],
            services_by_name["Softgel Ext. / Pedicure / Foot Spa / Cat Eye"],
            services_by_name["Softgel Ext. / Gel Pedicure / Foot Spa"],
            services_by_name["Manicure / Pedicure / Foot Spa / Foot Reflex"],
            services_by_name["Manicure / Pedicure / Foot Spa / Foot Massage"],
            services_by_name["Manicure / Gel Pedicure / Foot Spa"],
            services_by_name["Gel Manicure / Pedicure / Foot Massage"],
            services_by_name["Gel Manicure / Pedicure / Gel Removal / Cat Eye"],
            services_by_name["Gel Manicure / Stone / Foot Reflex"],
            services_by_name["Gel Manicure / Pedicure / Nail Art / Foot Paraffin"],
            services_by_name["Gel Manicure / Pedicure / Foot Spa / Gel Removal / Cat Eye"],
            services_by_name["Eyelash Ext."],
            services_by_name["Eyebrow Threading / Eyelash Lift"],
            services_by_name["Kiddie Manicure / Pedicure / Foot Spa"],
        ]

        payment_methods = [
            Payment.Method.CASH,
            Payment.Method.CASH,
            Payment.Method.CASH,
            Payment.Method.GCASH,
            Payment.Method.GCASH,
            Payment.Method.BANK,   # BPI in the spreadsheet
            Payment.Method.CARD,   # Credit Card in the spreadsheet
        ]

        branches = [Appointment.Branch.PLARIDEL, Appointment.Branch.PLARIDEL, Appointment.Branch.RETIRO_QC]

        sale_seq = 1
        total_sales_created = 0
        total_appts_created = 0

        # Generate 60 days of history: day_offset from 59 down to 0 (today)
        for days_ago in range(59, -1, -1):
            tx_date = today - timedelta(days=days_ago)
            is_weekend = tx_date.weekday() in (4, 5, 6)  # Fri, Sat, Sun
            num_transactions = rng.randint(13, 18) if is_weekend else rng.randint(9, 13)

            for slot_idx in range(num_transactions):
                cust = rng.choice(customers)
                tech = rng.choice(tech_list)
                receiver = rng.choice(receivers)
                branch = rng.choice(branches)

                if is_weekend:
                    svc = rng.choice(weekend_packages)
                else:
                    svc = rng.choice(weekday_services if rng.random() < 0.7 else weekend_packages)

                # Optional add-on (Gel Removal or Nail Art)
                extra_svc = None
                if rng.random() < 0.18 and svc.price < Decimal("1000.00"):
                    extra_svc = rng.choice([
                        services_by_name["Gel Removal / Cleaning"],
                        services_by_name["Custom Nail Art / Chrome Add-on"],
                        services_by_name["Eyebrow Threading"],
                    ])

                hour = min(9 + (slot_idx % 10), 18)
                minute = 30 if (slot_idx % 2 == 1) else 0
                start_t = time(hour, minute)
                total_dur = svc.duration_minutes + (extra_svc.duration_minutes if extra_svc else 0)
                end_dt = datetime.combine(tx_date, start_t) + timedelta(minutes=total_dur)
                end_t = end_dt.time()

                is_walkin = rng.random() < 0.45
                walkin_ref = f"WLK-{tx_date.strftime('%Y%m%d')}-{sale_seq:04d}" if is_walkin else ""

                # Create corresponding completed Appointment for booked visits (and today's active pipeline)
                appt = None
                if not is_walkin or days_ago <= 3:
                    appt = Appointment.objects.create(
                        customer=cust,
                        customer_name_snapshot=cust.full_name,
                        customer_email_snapshot=cust.email,
                        customer_phone_snapshot=cust.phone,
                        branch=branch,
                        appointment_date=tx_date,
                        start_time=start_t,
                        end_time=end_t,
                        assigned_staff=tech,
                        booking_source=Appointment.BookingSource.WALK_IN if is_walkin else Appointment.BookingSource.PUBLIC,
                        status=Appointment.Status.COMPLETED,
                        promo_code_issued="GETNAILED10" if not is_walkin else "",
                        notes=f"[SEED-60D] Handled by {tech.first_name} ({branch})",
                        created_by=receiver,
                        verified_at=timezone.now(),
                    )
                    AppointmentService.objects.create(
                        appointment=appt,
                        service=svc,
                        service_name=svc.name,
                        duration_minutes=svc.duration_minutes,
                        price=svc.price,
                        status=AppointmentService.Status.COMPLETED,
                    )
                    if extra_svc:
                        AppointmentService.objects.create(
                            appointment=appt,
                            service=extra_svc,
                            service_name=extra_svc.name,
                            duration_minutes=extra_svc.duration_minutes,
                            price=extra_svc.price,
                            status=AppointmentService.Status.COMPLETED,
                        )
                    total_appts_created += 1

                subtotal = svc.price + (extra_svc.price if extra_svc else Decimal("0.00"))
                receipt_no = f"GN-SEED-{tx_date.strftime('%Y%m%d')}-{sale_seq:04d}"
                sale_seq += 1

                sale = Sale.objects.create(
                    receipt_number=receipt_no,
                    customer=cust,
                    customer_name_snapshot=cust.full_name,
                    walk_in_referral_number=walkin_ref or f"WLK-{tx_date.strftime('%Y%m%d')}-{sale_seq:04d}",
                    appointment=appt,
                    subtotal=subtotal,
                    discount_type=Sale.DiscountType.NONE,
                    discount_value=Decimal("0.00"),
                    discount_amount=Decimal("0.00"),
                    total=subtotal,
                    status=Sale.Status.COMPLETED,
                    cashier=receiver,
                    cashier_name_snapshot=receiver.get_full_name() or receiver.email,
                )
                SaleItem.objects.create(
                    sale=sale,
                    service=svc,
                    assigned_staff=tech,
                    service_name=svc.name,
                    service_category=svc.category.name,
                    staff_name=tech.first_name,
                    unit_price=svc.price,
                    quantity=1,
                    line_total=svc.price,
                    position=0,
                )
                if extra_svc:
                    SaleItem.objects.create(
                        sale=sale,
                        service=extra_svc,
                        assigned_staff=tech,
                        service_name=extra_svc.name,
                        service_category=extra_svc.category.name,
                        staff_name=tech.first_name,
                        unit_price=extra_svc.price,
                        quantity=1,
                        line_total=extra_svc.price,
                        position=1,
                    )

                pmethod = rng.choice(payment_methods)
                ref_code = ""
                if pmethod == Payment.Method.GCASH:
                    ref_code = f"GCASH-{rng.randint(10000000, 99999999)}"
                elif pmethod == Payment.Method.BANK:
                    ref_code = f"BPI-{rng.randint(10000000, 99999999)}"
                elif pmethod == Payment.Method.CARD:
                    ref_code = f"CARD-{rng.randint(1000, 9999)}"

                payment = Payment.objects.create(
                    sale=sale,
                    payment_method=pmethod,
                    amount_tendered=subtotal,
                    change=Decimal("0.00"),
                    reference=ref_code,
                    recorded_by=receiver,
                )

                tx_dt = timezone.make_aware(datetime.combine(tx_date, start_t))
                Sale.objects.filter(pk=sale.pk).update(created_at=tx_dt, updated_at=tx_dt)
                Payment.objects.filter(pk=payment.pk).update(created_at=tx_dt)
                total_sales_created += 1

        # Also seed Today & Upcoming Appointments (PENDING, APPROVED, ONGOING, RESCHEDULED, NO_SHOW)
        upcoming_specs = [
            (0, time(10, 30), customers[0], staff_users["Rita"], services_by_name["Gel Manicure / Cat Eye"], Appointment.Status.ONGOING, Appointment.Branch.PLARIDEL, ""),
            (0, time(11, 0), customers[1], staff_users["Nory"], services_by_name["Softgel Ext. / Cat Eye"], Appointment.Status.ONGOING, Appointment.Branch.PLARIDEL, ""),
            (0, time(13, 30), customers[2], staff_users["Jessa"], services_by_name["Manicure / Pedicure / Foot Spa"], Appointment.Status.APPROVED, Appointment.Branch.PLARIDEL, ""),
            (0, time(14, 0), customers[3], staff_users["Sheng"], services_by_name["Eyelash Ext."], Appointment.Status.APPROVED, Appointment.Branch.RETIRO_QC, ""),
            (0, time(15, 0), customers[4], staff_users["Verna"], services_by_name["Gel Manicure / Pedicure"], Appointment.Status.PENDING, Appointment.Branch.PLARIDEL, ""),
            (0, time(16, 0), customers[5], staff_users["Aila"], services_by_name["Softgel Ext. / Gel Pedicure / Foot Spa"], Appointment.Status.PENDING, Appointment.Branch.RETIRO_QC, ""),
            (1, time(10, 0), customers[6], staff_users["Manilyn"], services_by_name["Manicure / Pedicure / Foot Spa / Foot Massage"], Appointment.Status.APPROVED, Appointment.Branch.PLARIDEL, ""),
            (1, time(13, 0), customers[7], staff_users["Ann"], services_by_name["Gel Manicure / Pedicure / Nail Art / Foot Paraffin"], Appointment.Status.PENDING, Appointment.Branch.PLARIDEL, ""),
            (2, time(11, 0), customers[8], staff_users["Stef"], services_by_name["Softgel Ext."], Appointment.Status.APPROVED, Appointment.Branch.RETIRO_QC, ""),
            (2, time(14, 30), customers[9], staff_users["Rita"], services_by_name["Deluxe Pedicure / Foot Spa"], Appointment.Status.RESCHEDULED, Appointment.Branch.PLARIDEL, "Client requested afternoon slot due to traffic in Tabang."),
            (0, time(9, 30), customers[10], staff_users["Nory"], services_by_name["Gel Manicure"], Appointment.Status.NO_SHOW, Appointment.Branch.PLARIDEL, "Auto-released after 15 mins grace period: No response to call/SMS reminder."),
        ]

        for day_add, st_time, cust, tech, svc, status, branch, reason in upcoming_specs:
            appt_date = today + timedelta(days=day_add)
            end_dt = datetime.combine(appt_date, st_time) + timedelta(minutes=svc.duration_minutes)
            appt = Appointment.objects.create(
                customer=cust,
                customer_name_snapshot=cust.full_name,
                customer_email_snapshot=cust.email,
                customer_phone_snapshot=cust.phone,
                branch=branch,
                appointment_date=appt_date,
                start_time=st_time,
                end_time=end_dt.time(),
                assigned_staff=tech,
                booking_source=Appointment.BookingSource.PUBLIC,
                status=status,
                promo_code_issued="GETNAILED10" if status in (Appointment.Status.APPROVED, Appointment.Status.ONGOING) else "",
                cancellation_reason=reason,
                notes=f"[SEED-60D] {svc.name} with {tech.first_name}",
                created_by=cust.user or owner,
                verified_at=timezone.now(),
            )
            svc_status = AppointmentService.Status.PENDING
            if status == Appointment.Status.ONGOING:
                svc_status = AppointmentService.Status.ONGOING
            elif status == Appointment.Status.APPROVED:
                svc_status = AppointmentService.Status.APPROVED
            elif status == Appointment.Status.COMPLETED:
                svc_status = AppointmentService.Status.COMPLETED

            AppointmentService.objects.create(
                appointment=appt,
                service=svc,
                service_name=svc.name,
                duration_minutes=svc.duration_minutes,
                price=svc.price,
                status=svc_status,
            )
            total_appts_created += 1

        # Update ReceiptSequence
        seq_obj, _ = ReceiptSequence.objects.get_or_create(pk=1)
        if seq_obj.last_value < sale_seq:
            seq_obj.last_value = sale_seq
            seq_obj.save(update_fields=["last_value", "updated_at"])

        return total_sales_created, total_appts_created

    def _seed_messages_and_notifications(self, owner, staff_users, customers):
        SalonMessage.objects.all().delete()
        c1 = customers[0]
        c2 = customers[1]
        SalonMessage.objects.create(
            customer=c1,
            sender=c1.user,
            sender_name=c1.full_name,
            body="Hi Miss Trina! Do you have Cat Eye Softgel Extensions available at Plaridel Rocka branch today?",
            is_from_customer=True,
            is_read=True,
        )
        SalonMessage.objects.create(
            customer=c1,
            sender=owner,
            sender_name="Trina Ocampo (Owner)",
            body="Hello Camille! Yes, Nory and Rita are available at our Plaridel Rocka Commercial Complex studio. Your promo code GETNAILED10 is also active!",
            is_from_customer=False,
            is_read=False,
        )
        SalonMessage.objects.create(
            customer=c2,
            sender=c2.user,
            sender_name=c2.full_name,
            body="Hello po, magkano po Manicure / Pedicure / Foot Spa package and need po ba ng deposit?",
            is_from_customer=True,
            is_read=True,
        )
        SalonMessage.objects.create(
            customer=c2,
            sender=owner,
            sender_name="Get Nailed Concierge",
            body="Hi Clarisse! Manicure / Pedicure / Foot Spa is ₱470. No advance payment is required—we just send a reminder 4–5 hours before and hold the slot for 10–15 minutes.",
            is_from_customer=False,
            is_read=False,
        )
