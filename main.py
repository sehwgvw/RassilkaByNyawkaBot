import asyncio
import logging
import os
import re
import sys
import random
import time
from datetime import datetime, timedelta
from typing import List, Optional, Union, Dict, Set
import traceback
import json
import base64
from io import BytesIO

# Сторонние библиотеки
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, CallbackQuery, InputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telethon import TelegramClient, functions, errors, utils
from telethon.tl.types import InputPeerChannel, InputPeerUser, Dialog, Chat, Channel
from telethon.tl.functions import messages, channels
# Импорт модуля для работы с папками
try:
    from telethon.tl.functions.chatlists import (
        GetExportedChatlistFilters, 
        DeleteExportedChatlist, 
        CheckChatlistInvite, 
        JoinChatlistInvite
    )
    from telethon.tl.types.chatlists import ChatlistInviteAlready
    CHATLISTS_AVAILABLE = True
except ImportError:
    CHATLISTS_AVAILABLE = False
    logger = logging.getLogger("MarketingBot")
    logger.warning("Библиотека не поддерживает папки (chatlists). Функционал будет ограничен.")

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, 
    ForeignKey, select, update, func, BigInteger, and_
)
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

# --- КОНФИГУРАЦИЯ (Вставлены ваши данные) ---
CONFIG = {
    "API_ID": 26563600,
    "API_HASH": '6f2a89308be7e5f8f8702b7811232840',
    "BOT_TOKEN": '8400853698:AAFyGyQeyUUBrCJXkmj3uEbfXx8TSHeFl6M',
    "ADMIN_IDS": [7544069555],
    
    # Экономика и настройки
    "BROADCAST_COST": 100.0,  # Увеличено до 100 рублей
    "REWARD_PUBLIC": 5.0,
    "REWARD_ADDLIST": 10.0,
    "MAX_ACCOUNTS": 10,
    "MAX_CHATS": 1000,
    "DELAY_BETWEEN_MSGS": 5, # сек
    
    # Футер для сообщений
    "FOOTER_TEXT": "\n\n—\nОтправлено через t.me/UwUMarketingBot",
    
    # Пути
    "SESSIONS_DIR": "sessions",
    "DB_NAME": "marketing_bot_v1.1.db",  # Новая БД
    "BANNER_PATH": "banner.png"
}

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot_log.txt", encoding='utf-8')
    ]
)
logger = logging.getLogger("MarketingBot")

# Создаем папки
if not os.path.exists(CONFIG["SESSIONS_DIR"]):
    os.makedirs(CONFIG["SESSIONS_DIR"])

# Удаляем старую БД если она существует
if os.path.exists("marketing_bot.db"):
    logger.info("Удаляю старую базу данных...")
    os.remove("marketing_bot.db")

# --- БАЗА ДАННЫХ (SQLAlchemy) ---
Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, index=True)
    username = Column(String, nullable=True)
    balance = Column(Float, default=0.0)
    reg_date = Column(DateTime, default=datetime.utcnow)
    is_admin = Column(Boolean, default=False)
    total_deposited = Column(Float, default=0.0)
    last_active = Column(DateTime, default=datetime.utcnow)

class Session(Base):
    __tablename__ = 'sessions'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id'))
    session_filename = Column(String, unique=True)
    phone = Column(String, nullable=True)
    username = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_premium = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id'))
    session_id = Column(Integer, ForeignKey('sessions.id'), nullable=True)
    link = Column(String, nullable=False)
    chat_type = Column(String, default="public") # public, private, addlist, from_folder
    chat_tg_id = Column(BigInteger, nullable=True)
    title = Column(String, nullable=True)
    username = Column(String, nullable=True) # Для быстрого доступа
    is_active = Column(Boolean, default=True) # Удалось ли войти
    from_folder = Column(String, nullable=True) # Из какой папки был добавлен
    added_at = Column(DateTime, default=datetime.utcnow)

class Broadcast(Base):
    __tablename__ = 'broadcasts'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id'))
    message_text = Column(String, nullable=False)
    status = Column(String, default="pending") # pending, processing, completed, failed
    total_chats = Column(Integer, default=0)
    success_count = Column(Integer, default=0)
    fail_count = Column(Integer, default=0)
    cost = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, ForeignKey('users.user_id'))
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False) # reward, broadcast, deposit, withdrawal
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PromoCode(Base):
    __tablename__ = 'promo_codes'
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    amount = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)
    created_by = Column(BigInteger, nullable=True) # ID администратора
    created_at = Column(DateTime, default=datetime.utcnow)
    activated_by = Column(BigInteger, nullable=True)
    activated_at = Column(DateTime, nullable=True)

# Инициализация DB Engine
engine = create_async_engine(f"sqlite+aiosqlite:///{CONFIG['DB_NAME']}", echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База данных успешно инициализирована")

# --- УТИЛИТЫ ---

def get_welcome_message(is_new_user: bool = False) -> str:
    """Генерирует приветственное сообщение с баннером"""
    if is_new_user:
        return (
            "🎉 **ДОБРО ПОЖАЛОВАТЬ В UwU Marketing Bot v1.1!** 🎉\n\n"
            "🚀 **САМЫЙ МОЩНЫЙ ИНСТРУМЕНТ ДЛЯ МАРКЕТИНГА В TELEGRAM**\n\n"
            "✨ **ВОЗМОЖНОСТИ:**\n"
            "✅ Управление неограниченным числом аккаунтов\n"
            "✅ Автоматический вход в чаты и папки\n"
            "✅ Массовые рассылки по всем чатам\n"
            "✅ Награды за добавление контента\n"
            "✅ Полная автоматизация процессов\n\n"
            "💰 **СТОИМОСТЬ РАССЫЛКИ:** 100 RUB\n"
            "🎁 **НАГРАДЫ:** 5-10 RUB за каждый добавленный чат\n\n"
            "📊 **НАЧНИТЕ ЗАРАБАТЫВАТЬ УЖЕ СЕЙЧАС!**"
        )
    else:
        return (
            "👋 **С ВОЗВРАЩЕНИЕМ В UwU Marketing Bot!** 👋\n\n"
            "🚀 **ВАШ МАРКЕТИНГОВЫЙ ИНСТРУМЕНТ ГОТОВ К РАБОТЕ**\n\n"
            "📈 **СЕГОДНЯШНИЕ ВОЗМОЖНОСТИ:**\n"
            "• Управление аккаунтами и чатами\n"
            "• Запуск массовых рассылок\n"
            "• Пополнение баланса и бонусы\n"
            "• Детальная статистика\n\n"
        )

class TelethonManager:
    """Управление клиентами Telethon (Userbots)"""
    
    @staticmethod
    async def verify_session(file_path: str) -> dict:
        """Проверяет файл сессии и возвращает инфо"""
        client = None
        try:
            client = TelegramClient(file_path, CONFIG["API_ID"], CONFIG["API_HASH"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return {"valid": False, "error": "Не авторизован"}
            
            me = await client.get_me()
            info = {
                "valid": True,
                "phone": me.phone,
                "username": me.username,
                "is_premium": getattr(me, 'premium', False),
                "id": me.id
            }
            
            # Обновление профиля по ТЗ
            try:
                await client(functions.account.UpdateProfileRequest(
                    first_name="Рассылка от няшки",
                    about="Аккаунт пренадлежит @Nyawka_CuteUwU"
                ))
            except Exception as e:
                logger.warning(f"Не удалось обновить профиль: {e}")
                
            await client.disconnect()
            return info
        except Exception as e:
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
            return {"valid": False, "error": str(e)}

    @staticmethod
    async def get_account_chats(session_path: str) -> List[dict]:
        """Получает ВСЕ чаты/каналы/группы на аккаунте"""
        client = None
        all_chats = []
        try:
            client = TelegramClient(session_path, CONFIG["API_ID"], CONFIG["API_HASH"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return all_chats

            # Получаем все диалоги
            dialogs = await client.get_dialogs(limit=200)
            
            for dialog in dialogs:
                try:
                    # Пропускаем личные чаты с пользователями
                    if isinstance(dialog.entity, Channel):
                        chat_info = {
                            'id': dialog.entity.id,
                            'title': dialog.entity.title,
                            'username': getattr(dialog.entity, 'username', None),
                            'is_channel': dialog.entity.broadcast,
                            'is_group': not dialog.entity.broadcast,
                            'access_hash': dialog.entity.access_hash if hasattr(dialog.entity, 'access_hash') else None
                        }
                        
                        # Генерируем ссылку
                        if chat_info['username']:
                            link = f"https://t.me/{chat_info['username']}"
                        else:
                            link = f"tg://resolve?domain={chat_info['id']}"
                        
                        chat_info['link'] = link
                        all_chats.append(chat_info)
                        
                except Exception as e:
                    logger.error(f"Error processing dialog: {e}")
                    continue
            
            await client.disconnect()
        except Exception as e:
            logger.error(f"Error getting account chats: {e}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
        return all_chats

    @staticmethod
    async def process_addlist(session_path: str, addlist_link: str, extract_chats: bool = True) -> dict:
        """
        Обрабатывает добавление папки (addlist)
        1. Удаляет старые папки (если есть)
        2. Добавляет новую папку
        3. Извлекает чаты из папки (если extract_chats=True)
        """
        client = None
        result = {
            "success": False,
            "folder_added": False,
            "chats_extracted": [],
            "error": "",
            "folder_slug": None
        }
        
        if not CHATLISTS_AVAILABLE:
            result["error"] = "Библиотека не поддерживает папки"
            return result
        
        try:
            client = TelegramClient(session_path, CONFIG["API_ID"], CONFIG["API_HASH"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                result["error"] = "Не авторизован"
                return result
            
            # Извлекаем slug из ссылки
            slug = addlist_link.split('addlist/')[-1].split('?')[0]
            result["folder_slug"] = slug
            
            # 1. Проверяем существующие папки и удаляем их
            try:
                exported = await client(GetExportedChatlistFiltersRequest())
                
                for folder in exported.filters:
                    try:
                        await client(DeleteExportedChatlistRequest(
                            slug=folder.slug
                        ))
                        logger.info(f"Удалена папка: {folder.slug}")
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.warning(f"Ошибка при удалении папки {folder.slug}: {e}")
            except Exception as e:
                logger.warning(f"Ошибка при получении папок: {e}")
            
            # 2. Добавляем новую папку
            try:
                # Проверяем инвайт
                check_res = await client(CheckChatlistInviteRequest(slug=slug))
                
                if isinstance(check_res, ChatlistInviteAlready):
                    result["folder_added"] = True
                    folder_chats = check_res.chats
                else:
                    folder_chats = check_res.chats
                    peers = [utils.get_input_peer(c) for c in folder_chats]
                    
                    await client(JoinChatlistInviteRequest(
                        slug=slug,
                        peers=peers
                    ))
                    result["folder_added"] = True
                    logger.info(f"Добавлена папка: {slug}")
                
                # 3. Извлекаем чаты из папки
                if extract_chats and result["folder_added"]:
                    extracted_chats = []
                    for chat in folder_chats:
                        try:
                            chat_info = {
                                'id': chat.id,
                                'title': getattr(chat, 'title', 'Без названия'),
                                'username': getattr(chat, 'username', None),
                                'access_hash': getattr(chat, 'access_hash', None)
                            }
                            
                            if chat_info['username']:
                                link = f"https://t.me/{chat_info['username']}"
                            else:
                                link = f"tg://resolve?domain={chat_info['id']}"
                            
                            chat_info['link'] = link
                            extracted_chats.append(chat_info)
                            
                        except Exception as e:
                            logger.error(f"Error processing chat from folder: {e}")
                            continue
                    
                    result["chats_extracted"] = extracted_chats
                    logger.info(f"Извлечено {len(extracted_chats)} чатов из папки")
                
                result["success"] = True
                
            except errors.FloodWaitError as e:
                result["error"] = f"FloodWait {e.seconds} секунд"
                logger.warning(f"FloodWait при добавлении папки: {e.seconds}s")
            except errors.ChatlistInviteAlreadyError:
                result["folder_added"] = True
                result["success"] = True
            except Exception as e:
                result["error"] = f"Ошибка добавления папки: {str(e)[:200]}"
                logger.error(f"Error adding folder {slug}: {e}")
            
            await client.disconnect()
        except Exception as e:
            result["error"] = f"Общая ошибка: {str(e)[:200]}"
            logger.error(f"Global error in process_addlist: {e}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
        return result

    @staticmethod
    async def broadcast_to_all_chats(session_path: str, text: str) -> dict:
        """
        Рассылает сообщение по ВСЕМ чатам на аккаунте
        """
        client = None
        stats = {
            "total": 0,
            "success": 0,
            "fail": 0,
            "errors": []
        }
        
        try:
            client = TelegramClient(session_path, CONFIG["API_ID"], CONFIG["API_HASH"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                stats["errors"].append("Не авторизован")
                return stats
            
            # Получаем все диалоги
            dialogs = await client.get_dialogs(limit=None)
            stats["total"] = len(dialogs)
            
            footer = CONFIG.get("FOOTER_TEXT", "\n\n—\nОтправлено через Marketing Bot")
            full_text = text + footer
            
            # Фильтруем только каналы и группы
            broadcast_dialogs = []
            for dialog in dialogs:
                if isinstance(dialog.entity, Channel):
                    broadcast_dialogs.append(dialog)
            
            logger.info(f"Найдено {len(broadcast_dialogs)} каналов/групп для рассылки")
            
            # Отправляем сообщения
            for i, dialog in enumerate(broadcast_dialogs):
                try:
                    await client.send_message(
                        dialog.entity,
                        full_text,
                        link_preview=False
                    )
                    stats["success"] += 1
                    logger.info(f"Отправлено в {dialog.entity.title} ({i+1}/{len(broadcast_dialogs)})")
                    
                    # Случайная задержка
                    delay = random.uniform(CONFIG["DELAY_BETWEEN_MSGS"], CONFIG["DELAY_BETWEEN_MSGS"] + 3)
                    await asyncio.sleep(delay)
                    
                except errors.FloodWaitError as e:
                    error_msg = f"FloodWait {e.seconds}s для {getattr(dialog.entity, 'title', 'Unknown')}"
                    stats["errors"].append(error_msg)
                    stats["fail"] += 1
                    logger.warning(f"FloodWait: {e.seconds}s")
                    break
                    
                except errors.ChatWriteForbiddenError:
                    stats["fail"] += 1
                    logger.warning(f"Нет прав на отправку в {getattr(dialog.entity, 'title', 'Unknown')}")
                    
                except Exception as e:
                    stats["fail"] += 1
                    logger.error(f"Ошибка отправки в {getattr(dialog.entity, 'title', 'Unknown')}: {e}")
            
            await client.disconnect()
            logger.info(f"Рассылка завершена: успешно {stats['success']}, ошибок {stats['fail']}")
            
        except Exception as e:
            stats["errors"].append(f"Общая ошибка: {str(e)[:200]}")
            logger.error(f"Global error in broadcast_to_all_chats: {e}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
        
        return stats

    @staticmethod
    async def join_single_chat(session_path: str, link: str) -> dict:
        """Вход в одиночный чат (публичный или приватный)"""
        client = None
        result = {"success": False, "error": "", "chat_info": {}}
        
        try:
            client = TelegramClient(session_path, CONFIG["API_ID"], CONFIG["API_HASH"])
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                result["error"] = "Не авторизован"
                return result
            
            try:
                # PRIVATE / JOINCHAT / PLUS LINKS
                if '+' in link or 'joinchat' in link:
                    if '+' in link:
                        hash_arg = link.split('+')[-1].strip()
                    else:
                        hash_arg = link.split('joinchat/')[-1].strip().split('/')[0]
                    
                    await client(functions.messages.ImportChatInviteRequest(hash_arg))
                    result["success"] = True
                    
                # PUBLIC USERNAME
                else:
                    clean_link = link.replace('https://', '').replace('http://', '').replace('t.me/', '').replace('telegram.me/', '').replace('@', '')
                    
                    if '/' in clean_link:
                        username = clean_link.split('/')[0]
                    else:
                        username = clean_link
                    
                    username = username.split('?')[0]
                    
                    await client(functions.channels.JoinChannelRequest(username))
                    result["success"] = True
                
                # Если успешно, получаем информацию о чате
                if result["success"]:
                    try:
                        if '+' in link or 'joinchat' in link:
                            result["chat_info"] = {"link": link}
                        else:
                            clean_link = link.replace('https://', '').replace('http://', '').replace('t.me/', '').replace('telegram.me/', '').replace('@', '')
                            username = clean_link.split('/')[0].split('?')[0]
                            entity = await client.get_entity(username)
                            result["chat_info"] = {
                                "id": entity.id,
                                "title": getattr(entity, 'title', username),
                                "username": getattr(entity, 'username', None),
                                "link": link
                            }
                    except Exception as e:
                        logger.warning(f"Не удалось получить инфо о чате {link}: {e}")
                        result["chat_info"] = {"link": link}
            
            except errors.UserAlreadyParticipantError:
                result["success"] = True
                result["error"] = "Уже участник"
            except errors.FloodWaitError as e:
                result["error"] = f"FloodWait {e.seconds}s"
            except errors.InviteHashExpiredError:
                result["error"] = "Ссылка устарела"
            except errors.InviteHashInvalidError:
                result["error"] = "Неверная ссылка"
            except errors.ChannelInvalidError:
                result["error"] = "Канал не существует"
            except errors.ChannelPrivateError:
                result["error"] = "Канал приватный"
            except Exception as e:
                result["error"] = f"Ошибка: {str(e)[:100]}"
            
            await client.disconnect()
        except Exception as e:
            result["error"] = f"Общая ошибка: {str(e)[:100]}"
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
        
        return result

# --- BOT STATES ---
class BotStates(StatesGroup):
    upload_session = State()
    add_chats_method = State()
    add_chats_file = State()
    add_chats_text = State()
    broadcast_text = State()
    broadcast_confirm = State()
    add_addlist = State()
    promo_activate = State()
    deposit_amount = State()
    
    # Admin
    admin_create_promo = State()
    admin_broadcast = State()
    admin_add_balance = State()

# --- КЛАВИАТУРЫ ---
def get_main_kb(user: User = None):
    kb = [
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💳 Кошелек")],
        [KeyboardButton(text="📁 База чатов"), KeyboardButton(text="🤖 Мои аккаунты")],
        [KeyboardButton(text="🚀 Рассылка"), KeyboardButton(text="ℹ️ Информация")]
    ]
    
    if user and user.is_admin:
        kb.append([KeyboardButton(text="🔒 АДМИН-ПАНЕЛЬ")])
    
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_admin_kb():
    kb = [
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="💰 Управление балансами"), KeyboardButton(text="🎁 Промокоды")],
        [KeyboardButton(text="📢 Рассылка всем"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="🔙 В меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_chat_actions_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📥 Загрузить файл", callback_data="chat_upload_file"))
    builder.row(InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="chat_enter_text"))
    builder.row(InlineKeyboardButton(text="📁 Добавить папку", callback_data="chat_add_folder"))
    builder.row(InlineKeyboardButton(text="🚪 Войти в чаты", callback_data="chat_start_join"))
    return builder.as_markup()

def get_account_actions_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="acc_add"))
    builder.row(InlineKeyboardButton(text="🔄 Проверить все", callback_data="acc_check"))
    builder.row(InlineKeyboardButton(text="📊 Получить все чаты", callback_data="acc_get_chats"))
    return builder.as_markup()

def get_wallet_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="wallet_deposit"))
    builder.row(InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="wallet_promo"))
    builder.row(InlineKeyboardButton(text="📊 История операций", callback_data="wallet_history"))
    return builder.as_markup()

def get_deposit_amounts_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="100 RUB", callback_data="deposit_100"),
        InlineKeyboardButton(text="500 RUB", callback_data="deposit_500")
    )
    builder.row(
        InlineKeyboardButton(text="1000 RUB", callback_data="deposit_1000"),
        InlineKeyboardButton(text="5000 RUB", callback_data="deposit_5000")
    )
    builder.row(InlineKeyboardButton(text="✏️ Другая сумма", callback_data="deposit_custom"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="wallet_back"))
    return builder.as_markup()

def get_confirm_broadcast_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Запустить", callback_data="broadcast_confirm_yes"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="broadcast_confirm_no")
    )
    return builder.as_markup()

def get_broadcast_preview_kb():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📝 Изменить текст", callback_data="broadcast_edit"),
        InlineKeyboardButton(text="🚀 Запустить", callback_data="broadcast_start")
    )
    return builder.as_markup()

def get_admin_promo_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_promo_create"))
    builder.row(InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_promo_list"))
    builder.row(InlineKeyboardButton(text="🗑️ Удалить промокод", callback_data="admin_promo_delete"))
    return builder.as_markup()

def get_admin_users_kb():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👁️ Просмотр пользователей", callback_data="admin_users_view"))
    builder.row(InlineKeyboardButton(text="➕ Добавить баланс", callback_data="admin_users_add_balance"))
    builder.row(InlineKeyboardButton(text="➖ Списать баланс", callback_data="admin_users_remove_balance"))
    builder.row(InlineKeyboardButton(text="🔍 Найти пользователя", callback_data="admin_users_search"))
    return builder.as_markup()

# --- БОТ И ДИСПЕТЧЕР ---
bot = Bot(token=CONFIG["BOT_TOKEN"])
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

async def send_welcome_with_banner(chat_id: int, is_new_user: bool = False):
    """Отправляет приветственное сообщение с баннером"""
    welcome_text = get_welcome_message(is_new_user)
    
    try:
        # Пытаемся отправить баннер если он существует
        if os.path.exists(CONFIG["BANNER_PATH"]):
            # Используем правильный класс InputFile
            photo = types.FSInputFile(CONFIG["BANNER_PATH"])
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=welcome_text,
                parse_mode="Markdown"
            )
        else:
            # Если баннера нет, отправляем просто текст
            await bot.send_message(
                chat_id=chat_id,
                text=welcome_text,
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Ошибка при отправке баннера: {e}")
        # Если ошибка, отправляем просто текст
        await bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            parse_mode="Markdown"
        )

# --- ХЭНДЛЕРЫ ---

@router.message(CommandStart())
async def cmd_start(message: types.Message):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.user_id == message.from_user.id))
        user = result.scalar_one_or_none()
        
        is_admin = message.from_user.id in CONFIG["ADMIN_IDS"]
        is_new_user = False
        
        if not user:
            user = User(
                user_id=message.from_user.id, 
                username=message.from_user.username,
                is_admin=is_admin
            )
            session.add(user)
            await session.commit()
            is_new_user = True
        
        # Отправляем приветствие с баннером
        await send_welcome_with_banner(message.chat.id, is_new_user)
        
        # Ждем секунду перед отправкой клавиатуры
        await asyncio.sleep(1)
        
        await message.answer(
            "👇 **Выберите действие в меню:**",
            reply_markup=get_main_kb(user)
        )

@router.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == message.from_user.id))).scalar_one()
        accs_count = (await session.execute(select(func.count(Session.id)).where(Session.user_id == user.user_id))).scalar()
        chats_count = (await session.execute(select(func.count(Chat.id)).where(Chat.user_id == user.user_id))).scalar()
        
        text = (
            f"👤 **ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: `{user.user_id}`\n"
            f"💵 Баланс: `{user.balance:.2f} RUB`\n"
            f"📊 Всего пополнено: `{user.total_deposited:.2f} RUB`\n"
            f"🤖 Аккаунтов: {accs_count} / {CONFIG['MAX_ACCOUNTS']}\n"
            f"📁 Чатов: {chats_count} / {CONFIG['MAX_CHATS']}\n"
            f"📅 Регистрация: {user.reg_date.strftime('%d.%m.%Y')}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "💳 Кошелек")
async def show_wallet(message: types.Message):
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == message.from_user.id))).scalar_one()
        
        # Получаем последние 5 транзакций
        trans = (await session.execute(
            select(Transaction).where(Transaction.user_id == user.user_id)
            .order_by(Transaction.created_at.desc()).limit(5)
        )).scalars().all()
        
        history_text = "\n".join([f"{'🟢' if t.amount > 0 else '🔴'} {t.amount:.2f} RUB ({t.type})" for t in trans])
        if not history_text: history_text = "Операций нет"
        
        text = (
            f"💳 **ВАШ КОШЕЛЕК**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Баланс: `{user.balance:.2f} RUB`\n\n"
            f"📊 **Последние операции:**\n{history_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await message.answer(text, reply_markup=get_wallet_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "wallet_deposit")
async def wallet_deposit(callback: types.CallbackQuery):
    text = (
        "💰 **ПОПОЛНЕНИЕ БАЛАНСА**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите сумму для пополнения:\n\n"
        "*Примечание:* Пополнение через банковскую карту. "
        "После оплаты баланс пополнится автоматически."
    )
    await callback.message.edit_text(text, reply_markup=get_deposit_amounts_kb(), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("deposit_"))
async def process_deposit(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data
    
    if data == "deposit_custom":
        await callback.message.edit_text(
            "✏️ **ВВЕДИТЕ СУММУ ДЛЯ ПОПОЛНЕНИЯ**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Минимальная сумма: 50 RUB\n"
            "Максимальная сумма: 50000 RUB"
        )
        await state.set_state(BotStates.deposit_amount)
        await callback.answer()
        return
    
    # Обрабатываем фиксированные суммы
    amounts = {
        "deposit_100": 100,
        "deposit_500": 500,
        "deposit_1000": 1000,
        "deposit_5000": 5000
    }
    
    amount = amounts.get(data)
    if amount:
        await process_deposit_payment(callback, amount)
    else:
        await callback.answer("Неизвестная сумма")

async def process_deposit_payment(callback: types.CallbackQuery, amount: float):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        user.balance += amount
        user.total_deposited += amount
        
        # Записываем транзакцию
        trx = Transaction(
            user_id=user_id,
            amount=amount,
            type="deposit",
            description=f"Пополнение баланса на {amount} RUB"
        )
        session.add(trx)
        await session.commit()
    
    await callback.message.edit_text(
        f"✅ **БАЛАНС ПОПОЛНЕН!**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Сумма: {amount} RUB\n"
        f"📊 Новый баланс: {user.balance:.2f} RUB\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Спасибо за использование нашего сервиса! 🎉"
    )
    await callback.answer()

@router.message(BotStates.deposit_amount)
async def process_custom_deposit(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        
        if amount < 50:
            await message.answer("❌ Минимальная сумма пополнения: 50 RUB")
            return
        
        if amount > 50000:
            await message.answer("❌ Максимальная сумма пополнения: 50000 RUB")
            return
        
        # Процесс пополнения
        async with async_session() as session:
            user = (await session.execute(select(User).where(User.user_id == message.from_user.id))).scalar_one()
            user.balance += amount
            user.total_deposited += amount
            
            trx = Transaction(
                user_id=message.from_user.id,
                amount=amount,
                type="deposit",
                description=f"Пополнение баланса на {amount} RUB"
            )
            session.add(trx)
            await session.commit()
        
        await message.answer(
            f"✅ **БАЛАНС ПОПОЛНЕН!**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Сумма: {amount} RUB\n"
            f"📊 Новый баланс: {user.balance:.2f} RUB\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Спасибо за использование нашего сервиса! 🎉"
        )
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную сумму (например: 500)")
    
    await state.clear()

@router.callback_query(F.data == "wallet_promo")
async def wallet_promo(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🎁 **АКТИВАЦИЯ ПРОМОКОДА**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите промокод для активации:"
    )
    await state.set_state(BotStates.promo_activate)
    await callback.answer()

@router.message(BotStates.promo_activate)
async def process_promo_code(message: types.Message, state: FSMContext):
    promo_code = message.text.strip().upper()
    user_id = message.from_user.id
    
    async with async_session() as session:
        # Ищем промокод
        promo = (await session.execute(
            select(PromoCode).where(
                PromoCode.code == promo_code,
                PromoCode.is_active == True,
                PromoCode.activated_by == None
            )
        )).scalar_one_or_none()
        
        if not promo:
            await message.answer("❌ Промокод не найден или уже использован")
            await state.clear()
            return
        
        # Активируем промокод
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        user.balance += promo.amount
        
        promo.is_active = False
        promo.activated_by = user_id
        promo.activated_at = datetime.utcnow()
        
        # Записываем транзакцию
        trx = Transaction(
            user_id=user_id,
            amount=promo.amount,
            type="deposit",
            description=f"Активация промокода {promo_code}"
        )
        session.add(trx)
        await session.commit()
    
    await message.answer(
        f"✅ **ПРОМОКОД АКТИВИРОВАН!**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎁 Промокод: {promo_code}\n"
        f"💰 Начислено: {promo.amount:.2f} RUB\n"
        f"📊 Новый баланс: {user.balance:.2f} RUB\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await state.clear()

@router.callback_query(F.data == "wallet_history")
async def wallet_history(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with async_session() as session:
        # Получаем последние 10 транзакций
        trans = (await session.execute(
            select(Transaction).where(Transaction.user_id == user_id)
            .order_by(Transaction.created_at.desc()).limit(10)
        )).scalars().all()
        
        if not trans:
            await callback.message.edit_text(
                "📊 **ИСТОРИЯ ОПЕРАЦИЙ**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Операций нет"
            )
            await callback.answer()
            return
        
        history_text = "📊 **ИСТОРИЯ ОПЕРАЦИЙ**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        for t in trans:
            date_str = t.created_at.strftime("%d.%m %H:%M")
            emoji = "🟢" if t.amount > 0 else "🔴"
            history_text += f"{emoji} {date_str}: {t.amount:+.2f} RUB ({t.type})\n"
            if t.description:
                history_text += f"   📝 {t.description}\n"
            history_text += "─" * 20 + "\n"
        
        await callback.message.edit_text(history_text)
    
    await callback.answer()

@router.callback_query(F.data == "wallet_back")
async def wallet_back(callback: types.CallbackQuery):
    await show_wallet(callback.message)
    await callback.answer()

# --- ЛОГИКА АККАУНТОВ ---

@router.message(F.text == "🤖 Мои аккаунты")
async def show_accounts(message: types.Message):
    async with async_session() as session:
        accs = (await session.execute(select(Session).where(Session.user_id == message.from_user.id))).scalars().all()
        
        text = "🤖 **УПРАВЛЕНИЕ АККАУНТАМИ**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        if not accs:
            text += "У вас нет подключенных аккаунтов."
        else:
            for i, acc in enumerate(accs, 1):
                status = "✅ Активен" if acc.is_active else "❌ Неактивен"
                prem = "🌟 Premium" if acc.is_premium else "👤 Free"
                phone_display = acc.phone if acc.phone else "Без номера"
                text += f"{i}. {phone_display} | {prem} | {status}\n"
        
        await message.answer(text, reply_markup=get_account_actions_kb())

@router.callback_query(F.data == "acc_add")
async def start_add_account(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📤 **ЗАГРУЗКА СЕССИИ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Отправьте мне файл `.session`, сгенерированный через Telethon.\n"
        "Файл будет проверен, и если он валиден, аккаунт добавится в базу."
    )
    await state.set_state(BotStates.upload_session)
    await callback.answer()

@router.message(BotStates.upload_session, F.document)
async def process_session_file(message: types.Message, state: FSMContext):
    if not message.document.file_name.endswith('.session'):
        await message.answer("❌ Это не файл .session. Попробуйте еще раз.")
        return

    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path
    
    local_filename = f"{message.from_user.id}_{int(time.time())}.session"
    dest_path = os.path.join(CONFIG["SESSIONS_DIR"], local_filename)
    
    await bot.download_file(file_path, dest_path)
    
    msg = await message.answer("🔄 Проверяю валидность сессии...")
    
    info = await TelethonManager.verify_session(dest_path)
    
    if info["valid"]:
        async with async_session() as session:
            count = (await session.execute(select(func.count(Session.id)).where(Session.user_id == message.from_user.id))).scalar()
            if count >= CONFIG["MAX_ACCOUNTS"]:
                os.remove(dest_path)
                await msg.edit_text("❌ Достигнут лимит аккаунтов (10 шт).")
                await state.clear()
                return

            new_session = Session(
                user_id=message.from_user.id,
                session_filename=local_filename,
                phone=info["phone"],
                username=info["username"],
                is_premium=info["is_premium"]
            )
            session.add(new_session)
            await session.commit()
        
        await msg.edit_text(
            "✅ **АККАУНТ УСПЕШНО ДОБАВЛЕН!**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Профиль обновлен: имя и био установлены."
        )
    else:
        os.remove(dest_path)
        await msg.edit_text(
            f"❌ **ОШИБКА СЕССИИ:** {info.get('error')}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Убедитесь, что сессия не завершена и 2FA не мешает."
        )
    
    await state.clear()

@router.callback_query(F.data == "acc_get_chats")
async def get_all_account_chats(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    msg = await callback.message.edit_text("🔄 Получаю все чаты со всех аккаунтов...")
    
    async with async_session() as session:
        accs = (await session.execute(
            select(Session).where(Session.user_id == user_id, Session.is_active == True)
        )).scalars().all()
        
        if not accs:
            await msg.edit_text("❌ Нет активных аккаунтов.")
            await callback.answer()
            return
        
        total_chats = 0
        
        for acc in accs:
            try:
                session_path = os.path.join(CONFIG["SESSIONS_DIR"], acc.session_filename)
                chats = await TelethonManager.get_account_chats(session_path)
                
                for chat in chats:
                    existing = (await session.execute(
                        select(Chat).where(
                            Chat.user_id == user_id,
                            Chat.session_id == acc.id,
                            Chat.chat_tg_id == chat['id']
                        )
                    )).scalar_one_or_none()
                    
                    if not existing:
                        new_chat = Chat(
                            user_id=user_id,
                            session_id=acc.id,
                            link=chat['link'],
                            chat_type="from_account",
                            chat_tg_id=chat['id'],
                            title=chat['title'],
                            username=chat['username'],
                            is_active=True
                        )
                        session.add(new_chat)
                        total_chats += 1
                
                await session.commit()
                
            except Exception as e:
                logger.error(f"Error getting chats for account {acc.id}: {e}")
                continue
        
        await msg.edit_text(
            f"✅ **ПОЛУЧЕНО {total_chats} ЧАТОВ**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Чаты успешно сохранены в базу данных."
        )
    
    await callback.answer()

# --- ЛОГИКА ЧАТОВ ---

@router.message(F.text == "📁 База чатов")
async def show_chats(message: types.Message):
    async with async_session() as session:
        count = (await session.execute(select(func.count(Chat.id)).where(Chat.user_id == message.from_user.id))).scalar()
        active_count = (await session.execute(select(func.count(Chat.id)).where(Chat.user_id == message.from_user.id, Chat.is_active == True))).scalar()
        
        text = (
            f"📁 **УПРАВЛЕНИЕ БАЗОЙ ЧАТОВ**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Всего чатов: {count}\n"
            f"Активных (вошли): {active_count}\n\n"
            f"💰 *Награда за добавление:* 5 руб/чат, 10 руб/папка."
        )
        await message.answer(text, reply_markup=get_chat_actions_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "chat_add_folder")
async def ask_addlist_link(callback: types.CallbackQuery, state: FSMContext):
    # Проверяем доступность функционала папок
    if not CHATLISTS_AVAILABLE:
        await callback.message.edit_text(
            "❌ **ФУНКЦИОНАЛ ПАПОК НЕДОСТУПЕН**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Ваша версия Telethon не поддерживает работу с папками.\n\n"
            "Пожалуйста, обновите библиотеку Telethon:\n"
            "`pip install --upgrade telethon`\n\n"
            "Требуется версия 1.28.0 или выше."
        )
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "📁 **ДОБАВЛЕНИЕ ПАПКИ (ADDLIST)**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Отправьте ссылку на папку в формате:\n"
        "`https://t.me/addlist/xxxxxxxxxx`\n\n"
        "*Бот выполнит следующие действия:*\n"
        "1. Проверит и удалит существующие папки на аккаунтах\n"
        "2. Добавит новую папку\n"
        "3. Сохранит все чаты из папки в базу\n"
        "4. Начислит награду за каждый чат"
    )
    await state.set_state(BotStates.add_addlist)
    await callback.answer()

@router.message(BotStates.add_addlist)
async def process_addlist_link(message: types.Message, state: FSMContext):
    # Проверяем доступность функционала папок
    if not CHATLISTS_AVAILABLE:
        await message.answer(
            "❌ **ФУНКЦИОНАЛ ПАПОК НЕДОСТУПЕН**\n"
            "Пожалуйста, обновите библиотеку Telethon."
        )
        await state.clear()
        return
    
    user_id = message.from_user.id
    addlist_link = message.text.strip()
    
    if 'addlist/' not in addlist_link:
        await message.answer("❌ Это не ссылка на папку (addlist). Попробуйте еще раз.")
        return
    
    msg = await message.answer("🔄 Обрабатываю папку... Это может занять несколько минут.")
    
    async with async_session() as session:
        accs = (await session.execute(
            select(Session).where(Session.user_id == user_id, Session.is_active == True)
        )).scalars().all()
        
        if not accs:
            await msg.edit_text("❌ Нет активных аккаунтов.")
            await state.clear()
            return
        
        total_chats_added = 0
        total_reward = 0
        results_summary = []
        
        for i, acc in enumerate(accs):
            try:
                session_path = os.path.join(CONFIG["SESSIONS_DIR"], acc.session_filename)
                
                await msg.edit_text(
                    f"🔄 Обработка аккаунта {i+1}/{len(accs)}...\n"
                    f"Добавлено чатов: {total_chats_added}"
                )
                
                result = await TelethonManager.process_addlist(session_path, addlist_link, extract_chats=True)
                
                if result["success"] and result["folder_added"]:
                    chats_added = 0
                    for chat_info in result["chats_extracted"]:
                        existing = (await session.execute(
                            select(Chat).where(
                                Chat.user_id == user_id,
                                Chat.session_id == acc.id,
                                Chat.chat_tg_id == chat_info['id']
                            )
                        )).scalar_one_or_none()
                        
                        if not existing:
                            new_chat = Chat(
                                user_id=user_id,
                                session_id=acc.id,
                                link=chat_info['link'],
                                chat_type="from_folder",
                                chat_tg_id=chat_info['id'],
                                title=chat_info['title'],
                                username=chat_info['username'],
                                is_active=True,
                                from_folder=result["folder_slug"]
                            )
                            session.add(new_chat)
                            chats_added += 1
                            total_chats_added += 1
                    
                    reward = CONFIG["REWARD_PUBLIC"] * chats_added + CONFIG["REWARD_ADDLIST"]
                    total_reward += reward
                    
                    results_summary.append({
                        "account": acc.phone or f"Аккаунт {i+1}",
                        "status": "✅ Успешно",
                        "chats": chats_added,
                        "reward": reward
                    })
                    
                    logger.info(f"Аккаунт {acc.id}: добавлено {chats_added} чатов из папки")
                    
                else:
                    results_summary.append({
                        "account": acc.phone or f"Аккаунт {i+1}",
                        "status": f"❌ Ошибка: {result['error'][:50]}",
                        "chats": 0,
                        "reward": 0
                    })
                
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.error(f"Error processing addlist for account {acc.id}: {e}")
                results_summary.append({
                    "account": acc.phone or f"Аккаунт {i+1}",
                    "status": f"❌ Ошибка: {str(e)[:50]}",
                    "chats": 0,
                    "reward": 0
                })
                continue
        
        if total_reward > 0:
            user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one()
            user.balance += total_reward
            
            trx = Transaction(
                user_id=user_id,
                amount=total_reward,
                type="reward",
                description=f"Добавлена папка и {total_chats_added} чатов"
            )
            session.add(trx)
            await session.commit()
        
        result_text = f"📊 **РЕЗУЛЬТАТЫ ОБРАБОТКИ ПАПКИ**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for res in results_summary:
            result_text += f"• {res['account']}: {res['status']}\n"
            if res['chats'] > 0:
                result_text += f"  Чатов: {res['chats']}, Награда: {res['reward']:.2f} руб\n"
        
        result_text += f"\n💰 **ИТОГО:**\n"
        result_text += f"• Чатов добавлено: {total_chats_added}\n"
        result_text += f"• Начислено: {total_reward:.2f} руб\n"
        result_text += f"• Новый баланс: {user.balance:.2f} руб"
        
        await msg.edit_text(result_text)
    
    await state.clear()

@router.callback_query(F.data == "chat_enter_text")
async def ask_chat_text(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✍️ **ВВЕДИТЕ ССЫЛКИ НА ЧАТЫ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Поддерживаются:\n"
        "- Публичные: `@username`, `t.me/username`\n"
        "- Приватные: `t.me/+hash`, `t.me/joinchat/...`\n"
        "- Папки: `t.me/addlist/...`\n"
        "Отправьте список одним сообщением (каждая ссылка с новой строки)."
    )
    await state.set_state(BotStates.add_chats_text)
    await callback.answer()

@router.message(BotStates.add_chats_text)
async def process_chat_text(message: types.Message, state: FSMContext):
    lines = message.text.strip().split('\n')
    links = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if re.match(r'https?://(?:t\.me|telegram\.me)/', line):
            links.append(line)
        elif re.match(r'^@[a-zA-Z0-9_]+$', line):
            links.append(f"https://t.me/{line[1:]}")
        elif re.match(r'^t\.me/[a-zA-Z0-9_]+', line):
            links.append(f"https://{line}")
        elif '+' in line or 'joinchat' in line:
            if 't.me/' in line:
                links.append(f"https://{line}" if not line.startswith('http') else line)
            else:
                if '+' in line:
                    links.append(f"https://t.me/+{line.replace('+', '')}")
                elif 'joinchat/' in line:
                    links.append(f"https://t.me/{line}")
        elif 'addlist/' in line:
            if 't.me/' in line:
                links.append(f"https://{line}" if not line.startswith('http') else line)
            else:
                links.append(f"https://t.me/{line}")
    
    if not links:
        await message.answer("❌ Ссылки не найдены в правильном формате.")
        return
    
    links = list(set(links))
    
    regular_links = [link for link in links if 'addlist/' not in link]
    addlist_links = [link for link in links if 'addlist/' in link]
    
    if regular_links:
        await process_new_chats(message.from_user.id, regular_links, message)
    
    if addlist_links and CHATLISTS_AVAILABLE:
        await message.answer(f"📁 Найдено {len(addlist_links)} папок. Обрабатываю первую папку...")
        # Сохраняем первую ссылку на папку
        await state.update_data(addlist_link=addlist_links[0])
        await process_addlist_link(message, state)
    elif addlist_links and not CHATLISTS_AVAILABLE:
        await message.answer("❌ Функционал папок недоступен. Обновите Telethon.")
    
    await state.clear()

async def process_new_chats(user_id: int, links: List[str], message: types.Message):
    added_count = 0
    reward = 0.0
    
    async with async_session() as session:
        current_count = (await session.execute(select(func.count(Chat.id)).where(Chat.user_id == user_id))).scalar()
        
        for link in links:
            if current_count >= CONFIG["MAX_CHATS"]:
                break
                
            exists = (await session.execute(select(Chat).where(Chat.user_id == user_id, Chat.link == link))).scalar()
            if exists:
                continue
                
            new_chat = Chat(user_id=user_id, link=link, chat_type="public", is_active=False)
            session.add(new_chat)
            
            reward += CONFIG["REWARD_PUBLIC"]
            added_count += 1
            current_count += 1
        
        if added_count > 0:
            user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one()
            user.balance += reward
            
            trx = Transaction(user_id=user_id, amount=reward, type="reward", description=f"Добавлено {added_count} чатов")
            session.add(trx)
            await session.commit()
            
            await message.answer(
                f"✅ **ДОБАВЛЕНО: {added_count} ЧАТОВ**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Начислено: {reward:.2f} руб."
            )
        else:
            await message.answer("⚠️ Новые чаты не добавлены (дубликаты или лимит).")

@router.callback_query(F.data == "chat_start_join")
async def start_joining(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    try:
        await callback.answer("Начинаю вход в чаты...")
    except Exception as e:
        logger.debug(f"Ignoring callback answer error: {e}")
    
    msg = await callback.message.edit_text("⏳ Начинаю процесс входа в чаты... Это займет время.")
    
    async with async_session() as session:
        chats = (await session.execute(
            select(Chat).where(Chat.user_id == user_id, Chat.is_active == False).limit(50)
        )).scalars().all()
        
        if not chats:
            await msg.edit_text("✅ Все чаты уже обработаны.")
            return

        accs = (await session.execute(
            select(Session).where(Session.user_id == user_id, Session.is_active == True)
        )).scalars().all()
        
        if not accs:
            await msg.edit_text("❌ Нет активных аккаунтов для входа.")
            return
            
        total_chats = len(chats)
        progress_msg = await msg.edit_text(f"⏳ Обработка 0/{total_chats} чатов...")
        
        chunk_size = max(1, total_chats // len(accs))
        
        for i, acc in enumerate(accs):
            start_idx = i * chunk_size
            end_idx = start_idx + chunk_size if i < len(accs) - 1 else total_chats
            acc_chats = chats[start_idx:end_idx]
            
            if not acc_chats:
                continue
            
            for j, chat in enumerate(acc_chats):
                try:
                    session_path = os.path.join(CONFIG["SESSIONS_DIR"], acc.session_filename)
                    result = await TelethonManager.join_single_chat(session_path, chat.link)
                    
                    if result["success"]:
                        chat.is_active = True
                        
                        if result["chat_info"]:
                            if "id" in result["chat_info"]:
                                chat.chat_tg_id = result["chat_info"]["id"]
                            if "title" in result["chat_info"]:
                                chat.title = result["chat_info"]["title"]
                            if "username" in result["chat_info"]:
                                chat.username = result["chat_info"]["username"]
                    
                    current = i * chunk_size + j + 1
                    await progress_msg.edit_text(f"⏳ Обработка {current}/{total_chats} чатов...")
                    
                    await asyncio.sleep(random.uniform(3, 8))
                    
                except Exception as e:
                    logger.error(f"Error joining chat {chat.link}: {e}")
                    continue
        
        await session.commit()
        
        successful = sum(1 for chat in chats if chat.is_active)
        failed = total_chats - successful
        
        result_text = f"🚪 **РЕЗУЛЬТАТЫ ВХОДА:**\n✅ Успешно: {successful}\n❌ Ошибки: {failed}"
        await progress_msg.edit_text(result_text)

# --- ЛОГИКА РАССЫЛКИ ---

@router.message(F.text == "🚀 Рассылка")
async def start_broadcast_wizard(message: types.Message, state: FSMContext):
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == message.from_user.id))).scalar_one()
        
        active_accs = (await session.execute(
            select(func.count(Session.id)).where(Session.user_id == user.user_id, Session.is_active == True)
        )).scalar()
        
        if user.balance < CONFIG["BROADCAST_COST"]:
            await message.answer(
                f"❌ **НЕДОСТАТОЧНО СРЕДСТВ!**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Нужно: {CONFIG['BROADCAST_COST']:.2f} RUB\n"
                f"💳 Ваш баланс: {user.balance:.2f} RUB\n\n"
                f"Пополните баланс в разделе 'Кошелек' 💰"
            )
            return
            
        if active_accs == 0:
            await message.answer("❌ Нет активных аккаунтов для рассылки.")
            return
            
        await message.answer(
            "📝 **СОЗДАНИЕ РАССЫЛКИ**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "💎 *Стоимость одной рассылки: 100 RUB*\n\n"
            "*Важно:* Рассылка будет выполнена по ВСЕМ чатам на каждом аккаунте,\n"
            "независимо от того, есть они в базе данных или нет.\n\n"
            "✍️ **Введите текст вашего сообщения:**\n"
            "Поддерживается Markdown (*жирный*, `код`, [ссылка](...))."
        )
        await state.set_state(BotStates.broadcast_text)

@router.message(BotStates.broadcast_text)
async def broadcast_text_handler(message: types.Message, state: FSMContext):
    if len(message.text) > 4000:
        await message.answer("❌ Текст слишком длинный (максимум 4000 символов).")
        return
    
    await state.update_data(text=message.text)
    
    preview = message.text + CONFIG.get("FOOTER_TEXT", "\n\n—\nОтправлено через Marketing Bot")
    
    await message.answer(
        f"📣 **ПРЕДПРОСМОТР СООБЩЕНИЯ:**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{preview}\n\n"
        f"💎 **Стоимость:** {CONFIG['BROADCAST_COST']:.2f} RUB\n\n"
        f"⚠️ *Рассылка будет выполнена по ВСЕМ чатам на аккаунтах*",
        reply_markup=get_broadcast_preview_kb()
    )

@router.callback_query(F.data == "broadcast_edit")
async def broadcast_edit(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✍️ **ВВЕДИТЕ НОВЫЙ ТЕКСТ СООБЩЕНИЯ:**\n"
        "Поддерживается Markdown (*жирный*, `код`, [ссылка](...))."
    )
    await state.set_state(BotStates.broadcast_text)
    await callback.answer()

@router.callback_query(F.data == "broadcast_start")
async def broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get('text')
    
    if not text:
        await callback.answer("❌ Текст сообения не найден")
        return
    
    await callback.message.edit_text(
        f"🚀 **ПОДТВЕРЖДЕНИЕ РАССЫЛКИ**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Текст: {text[:100]}...\n"
        f"💎 Стоимость: {CONFIG['BROADCAST_COST']:.2f} RUB\n\n"
        f"⚠️ *Вы уверены, что хотите запустить рассылку?*",
        reply_markup=get_confirm_broadcast_kb()
    )
    await callback.answer()

@router.callback_query(F.data == "broadcast_confirm_yes")
async def broadcast_confirm_yes(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get('text')
    user_id = callback.from_user.id
    
    if not text:
        await callback.answer("❌ Текст сообщения не найден")
        return
    
    await callback.message.edit_text("🚀 Запуск рассылки... Списание средств...")
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one()
        
        active_accs = (await session.execute(
            select(Session).where(Session.user_id == user_id, Session.is_active == True)
        )).scalars().all()
        
        if not active_accs:
            await callback.message.edit_text("❌ Нет активных аккаунтов для рассылки.")
            await state.clear()
            return
        
        # Списание
        user.balance -= CONFIG["BROADCAST_COST"]
        trx = Transaction(
            user_id=user_id,
            amount=-CONFIG["BROADCAST_COST"],
            type="broadcast",
            description="Оплата рассылки по всем чатам"
        )
        session.add(trx)
        
        broadcast = Broadcast(
            user_id=user_id,
            message_text=text,
            total_chats=0,
            status="processing",
            cost=CONFIG["BROADCAST_COST"]
        )
        session.add(broadcast)
        await session.commit()
        
        broadcast_id = broadcast.id
        
        asyncio.create_task(
            safe_broadcast_to_all_accounts(broadcast_id, text, active_accs, user_id)
        )
    
    await callback.message.edit_text(
        "✅ **РАССЫЛКА ЗАПУЩЕНА!**\n\n"
        "📤 Отправляю сообщения по ВСЕМ чатам на аккаунтах...\n"
        "📊 Вы получите детальный отчет по завершении."
    )
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "broadcast_confirm_no")
async def broadcast_confirm_no(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("❌ Рассылка отменена.")
    await state.clear()
    await callback.answer()

async def safe_broadcast_to_all_accounts(broadcast_id, text, accs, user_id):
    """Безопасная рассылка по всем чатам на всех аккаунтах"""
    try:
        await broadcast_to_all_accounts(broadcast_id, text, accs, user_id)
    except Exception as e:
        logger.error(f"Error in broadcast process: {e}\n{traceback.format_exc()}")
        try:
            await bot.send_message(user_id, f"❌ Ошибка при выполнении рассылки: {str(e)[:200]}")
        except:
            pass

async def broadcast_to_all_accounts(broadcast_id, text, accs, user_id):
    """Рассылка по всем чатам на всех аккаунтах"""
    total_stats = {
        "accounts": len(accs),
        "total_sent": 0,
        "total_success": 0,
        "total_fail": 0,
        "account_stats": []
    }
    
    try:
        await bot.send_message(user_id, f"📤 Начинаю рассылку по {len(accs)} аккаунтам...")
    except:
        pass
    
    for i, acc in enumerate(accs):
        try:
            session_path = os.path.join(CONFIG["SESSIONS_DIR"], acc.session_filename)
            
            try:
                await bot.send_message(
                    user_id,
                    f"🔄 Аккаунт {i+1}/{len(accs)} ({acc.phone or 'Без номера'}): рассылка по всем чатам..."
                )
            except:
                pass
            
            stats = await TelethonManager.broadcast_to_all_chats(session_path, text)
            
            total_stats["total_sent"] += stats.get("total", 0)
            total_stats["total_success"] += stats.get("success", 0)
            total_stats["total_fail"] += stats.get("fail", 0)
            
            total_stats["account_stats"].append({
                "account": acc.phone or f"Аккаунт {i+1}",
                "stats": stats
            })
            
            logger.info(f"Аккаунт {acc.id}: отправлено {stats.get('success', 0)}/{stats.get('total', 0)}")
            
            await asyncio.sleep(10)
            
        except Exception as e:
            logger.error(f"Error broadcasting from account {acc.id}: {e}")
            total_stats["account_stats"].append({
                "account": acc.phone or f"Аккаунт {i+1}",
                "error": str(e)[:200]
            })
            continue
    
    async with async_session() as session:
        br = (await session.execute(select(Broadcast).where(Broadcast.id == broadcast_id))).scalar_one()
        br.status = "completed"
        br.total_chats = total_stats["total_sent"]
        br.success_count = total_stats["total_success"]
        br.fail_count = total_stats["total_fail"]
        br.completed_at = datetime.utcnow()
        await session.commit()
    
    report_text = f"📢 **РАССЫЛКА ЗАВЕРШЕНА!**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    report_text += f"📊 **ОБЩАЯ СТАТИСТИКА:**\n"
    report_text += f"• Аккаунтов: {total_stats['accounts']}\n"
    report_text += f"• Всего чатов: {total_stats['total_sent']}\n"
    report_text += f"• Успешно: {total_stats['total_success']}\n"
    report_text += f"• Ошибки: {total_stats['total_fail']}\n"
    
    if total_stats['total_sent'] > 0:
        efficiency = int((total_stats['total_success'] / total_stats['total_sent']) * 100)
        report_text += f"• Эффективность: {efficiency}%\n"
    
    report_text += f"\n💰 Стоимость: {CONFIG['BROADCAST_COST']:.2f} RUB\n\n"
    
    report_text += "📋 **СТАТИСТИКА ПО АККАУНТАМ:**\n"
    for acc_stat in total_stats["account_stats"]:
        if "stats" in acc_stat:
            stats = acc_stat["stats"]
            report_text += f"• {acc_stat['account']}: {stats.get('success', 0)}/{stats.get('total', 0)}\n"
        else:
            report_text += f"• {acc_stat['account']}: ❌ {acc_stat.get('error', 'Ошибка')}\n"
    
    try:
        await bot.send_message(user_id, report_text)
    except Exception as e:
        logger.error(f"Failed to send report to user: {e}")

# --- АДМИН ПАНЕЛЬ ---

@router.message(F.text == "🔒 АДМИН-ПАНЕЛЬ")
async def admin_panel(message: types.Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        
        if not user or not user.is_admin:
            await message.answer("❌ У вас нет доступа к админ-панели.")
            return
    
    admin_text = (
        "👨‍💻 **АДМИН-ПАНЕЛЬ v1.1**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 **ВОЗМОЖНОСТИ:**\n"
        "• Просмотр статистики системы\n"
        "• Управление пользователями\n"
        "• Пополнение/списание балансов\n"
        "• Создание и управление промокодами\n"
        "• Массовая рассылка всем пользователям\n"
        "• Настройки системы"
    )
    
    await message.answer(admin_text, reply_markup=get_admin_kb(), parse_mode="Markdown")

@router.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        
        if not user or not user.is_admin:
            await message.answer("❌ У вас нет доступа к админ-панели.")
            return
        
        # Общая статистика
        u_cnt = (await session.execute(select(func.count(User.id)))).scalar()
        s_cnt = (await session.execute(select(func.count(Session.id)))).scalar()
        c_cnt = (await session.execute(select(func.count(Chat.id)))).scalar()
        b_cnt = (await session.execute(select(func.count(Broadcast.id)))).scalar()
        
        # Статистика по доходам
        total_deposits = (await session.execute(
            select(func.sum(Transaction.amount)).where(Transaction.type == "deposit")
        )).scalar() or 0
        
        total_withdrawals = (await session.execute(
            select(func.sum(Transaction.amount)).where(Transaction.type == "broadcast")
        )).scalar() or 0
        
        # Активные пользователи за последние 7 дней
        week_ago = datetime.utcnow() - timedelta(days=7)
        active_users = (await session.execute(
            select(func.count(User.id)).where(User.reg_date >= week_ago)
        )).scalar()
        
        stats_text = (
            f"📊 **СИСТЕМНАЯ СТАТИСТИКА**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 **ПОЛЬЗОВАТЕЛИ:**\n"
            f"• Всего: {u_cnt}\n"
            f"• Активных (7 дней): {active_users}\n\n"
            f"🤖 **АККАУНТЫ:**\n"
            f"• Сессий: {s_cnt}\n\n"
            f"📁 **ЧАТЫ:**\n"
            f"• В базе: {c_cnt}\n\n"
            f"🚀 **РАССЫЛКИ:**\n"
            f"• Проведено: {b_cnt}\n\n"
            f"💰 **ФИНАНСЫ:**\n"
            f"• Всего пополнено: {abs(total_deposits):.2f} RUB\n"
            f"• Всего списано: {abs(total_withdrawals):.2f} RUB\n"
            f"• Чистая прибыль: {abs(total_deposits) - abs(total_withdrawals):.2f} RUB"
        )
        
        await message.answer(stats_text, parse_mode="Markdown")

@router.message(F.text == "👥 Пользователи")
async def admin_users(message: types.Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        
        if not user or not user.is_admin:
            await message.answer("❌ У вас нет доступа к админ-панели.")
            return
        
        await message.answer(
            "👥 **УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Выберите действие:",
            reply_markup=get_admin_users_kb()
        )

@router.callback_query(F.data == "admin_users_view")
async def admin_users_view(callback: types.CallbackQuery):
    async with async_session() as session:
        # Получаем последних 10 пользователей
        users = (await session.execute(
            select(User).order_by(User.reg_date.desc()).limit(10)
        )).scalars().all()
        
        if not users:
            await callback.message.edit_text("📭 Пользователей нет")
            await callback.answer()
            return
        
        users_text = "👥 **ПОСЛЕДНИЕ 10 ПОЛЬЗОВАТЕЛЕЙ:**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for user in users:
            reg_date = user.reg_date.strftime("%d.%m.%Y")
            users_text += f"👤 ID: {user.user_id}\n"
            users_text += f"📅 Регистрация: {reg_date}\n"
            users_text += f"💰 Баланс: {user.balance:.2f} RUB\n"
            users_text += f"👑 Админ: {'✅' if user.is_admin else '❌'}\n"
            users_text += "─" * 20 + "\n"
        
        await callback.message.edit_text(users_text)
    
    await callback.answer()

@router.callback_query(F.data == "admin_users_add_balance")
async def admin_users_add_balance(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➕ **ДОБАВЛЕНИЕ БАЛАНСА ПОЛЬЗОВАТЕЛЮ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите ID пользователя и сумму через пробел:\n"
        "Например: `123456789 500`\n\n"
        "*Примечание:* Сумма может быть отрицательной для списания."
    )
    await state.set_state(BotStates.admin_add_balance)
    await callback.answer()

@router.message(BotStates.admin_add_balance)
async def process_admin_add_balance(message: types.Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте: ID СУММА")
            return
        
        user_id_to = int(parts[0])
        amount = float(parts[1])
        
        if amount == 0:
            await message.answer("❌ Сумма не может быть нулевой")
            return
        
        async with async_session() as session:
            # Проверяем существование пользователя
            user = (await session.execute(
                select(User).where(User.user_id == user_id_to)
            )).scalar_one_or_none()
            
            if not user:
                await message.answer(f"❌ Пользователь с ID {user_id_to} не найден")
                return
            
            # Изменяем баланс
            user.balance += amount
            
            # Записываем транзакцию
            trx = Transaction(
                user_id=user_id_to,
                amount=amount,
                type="deposit" if amount > 0 else "withdrawal",
                description=f"Админское изменение баланса от {message.from_user.id}"
            )
            session.add(trx)
            await session.commit()
            
            if amount > 0:
                action = "добавлен"
            else:
                action = "списан"
                amount = abs(amount)
            
            await message.answer(
                f"✅ **БАЛАНС УСПЕШНО ИЗМЕНЕН!**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Пользователь: {user_id_to}\n"
                f"💰 Сумма {action}: {amount:.2f} RUB\n"
                f"📊 Новый баланс: {user.balance:.2f} RUB"
            )
    
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте: ID СУММА (например: 123456789 500)")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    
    await state.clear()

@router.message(F.text == "🎁 Промокоды")
async def admin_promocodes(message: types.Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        
        if not user or not user.is_admin:
            await message.answer("❌ У вас нет доступа к админ-панели.")
            return
        
        await message.answer(
            "🎁 **УПРАВЛЕНИЕ ПРОМОКОДАМИ**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Выберите действие:",
            reply_markup=get_admin_promo_kb()
        )

@router.callback_query(F.data == "admin_promo_create")
async def admin_promo_create(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➕ **СОЗДАНИЕ ПРОМОКОДА**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите код и сумму через пробел:\n"
        "Например: `SUMMER2024 1000`\n\n"
        "*Примечание:* Код будет автоматически приведен к верхнему регистру."
    )
    await state.set_state(BotStates.admin_create_promo)
    await callback.answer()

@router.message(BotStates.admin_create_promo)
async def process_admin_create_promo(message: types.Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        if len(parts) != 2:
            await message.answer("❌ Неверный формат. Используйте: КОД СУММА")
            return
        
        code = parts[0].upper()
        amount = float(parts[1])
        
        if amount <= 0:
            await message.answer("❌ Сумма должна быть больше 0")
            return
        
        async with async_session() as session:
            # Проверяем существование промокода
            existing = (await session.execute(
                select(PromoCode).where(PromoCode.code == code)
            )).scalar_one_or_none()
            
            if existing:
                await message.answer(f"❌ Промокод {code} уже существует")
                return
            
            # Создаем промокод
            promo = PromoCode(
                code=code,
                amount=amount,
                is_active=True,
                created_by=message.from_user.id
            )
            session.add(promo)
            await session.commit()
            
            await message.answer(
                f"✅ **ПРОМОКОД СОЗДАН!**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎁 Код: `{code}`\n"
                f"💰 Сумма: {amount:.2f} RUB\n"
                f"📅 Создан: {datetime.utcnow().strftime('%d.%m.%Y %H:%M')}\n\n"
                f"Промокод активен и готов к использованию!"
            )
    
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте: КОД СУММА (например: SUMMER2024 1000)")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")
    
    await state.clear()

@router.callback_query(F.data == "admin_promo_list")
async def admin_promo_list(callback: types.CallbackQuery):
    async with async_session() as session:
        # Получаем все промокоды
        promos = (await session.execute(
            select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)
        )).scalars().all()
        
        if not promos:
            await callback.message.edit_text("📭 Промокодов нет")
            await callback.answer()
            return
        
        promos_text = "🎁 **СПИСОК ПРОМОКОДОВ:**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for promo in promos:
            created_date = promo.created_at.strftime("%d.%m")
            status = "✅ Активен" if promo.is_active else "❌ Использован"
            
            if promo.activated_at:
                activated_date = promo.activated_at.strftime("%d.%m")
                activated_by = f"👤 {promo.activated_by}"
            else:
                activated_date = "Не использован"
                activated_by = ""
            
            promos_text += f"🎫 Код: `{promo.code}`\n"
            promos_text += f"💰 Сумма: {promo.amount:.2f} RUB\n"
            promos_text += f"📅 Создан: {created_date}\n"
            promos_text += f"📊 Статус: {status}\n"
            promos_text += f"📅 Активирован: {activated_date} {activated_by}\n"
            promos_text += "─" * 20 + "\n"
        
        await callback.message.edit_text(promos_text)
    
    await callback.answer()

@router.message(F.text == "📢 Рассылка всем")
async def admin_broadcast_all(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        
        if not user or not user.is_admin:
            await message.answer("❌ У вас нет доступа к админ-панели.")
            return
    
    await message.answer(
        "📢 **РАССЫЛКА ВСЕМ ПОЛЬЗОВАТЕЛЯМ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите текст сообщения для отправки всем пользователям бота:\n\n"
        "*Внимание:* Это сообщение будет отправлено всем пользователям!"
    )
    await state.set_state(BotStates.admin_broadcast)

@router.message(BotStates.admin_broadcast)
async def process_admin_broadcast(message: types.Message, state: FSMContext):
    text = message.text
    admin_id = message.from_user.id
    
    await message.answer("🔄 Начинаю рассылку всем пользователям...")
    
    async with async_session() as session:
        # Получаем всех пользователей
        users = (await session.execute(select(User))).scalars().all()
        
        total = len(users)
        success = 0
        failed = 0
        
        for user in users:
            try:
                # Пропускаем себя
                if user.user_id == admin_id:
                    continue
                
                await bot.send_message(
                    user.user_id,
                    f"📢 **СООБЩЕНИЕ ОТ АДМИНИСТРАТОРА:**\n\n{text}"
                )
                success += 1
                
                # Задержка между отправками
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Failed to send admin broadcast to {user.user_id}: {e}")
                failed += 1
                continue
        
        await message.answer(
            f"✅ **РАССЫЛКА ЗАВЕРШЕНА!**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Статистика:\n"
            f"• Всего пользователей: {total}\n"
            f"• Успешно отправлено: {success}\n"
            f"• Не удалось отправить: {failed}"
        )
    
    await state.clear()

@router.message(F.text == "💰 Управление балансами")
async def admin_balance_management(message: types.Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        
        if not user or not user.is_admin:
            await message.answer("❌ У вас нет доступа к админ-панели.")
            return
    
    # Получаем топ пользователей по балансу
    async with async_session() as session:
        top_users = (await session.execute(
            select(User).order_by(User.balance.desc()).limit(10)
        )).scalars().all()
        
        top_text = "💰 **ТОП ПОЛЬЗОВАТЕЛЕЙ ПО БАЛАНСУ:**\n━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for i, user in enumerate(top_users, 1):
            top_text += f"{i}. 👤 ID: {user.user_id}\n"
            top_text += f"   💰 Баланс: {user.balance:.2f} RUB\n"
            top_text += f"   📊 Пополнено: {user.total_deposited:.2f} RUB\n"
            if i < len(top_users):
                top_text += "─" * 20 + "\n"
        
        await message.answer(top_text)

@router.message(F.text == "⚙️ Настройки")
async def admin_settings(message: types.Message):
    user_id = message.from_user.id
    
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
        
        if not user or not user.is_admin:
            await message.answer("❌ У вас нет доступа к админ-панели.")
            return
    
    settings_text = (
        "⚙️ **НАСТРОЙКИ СИСТЕМЫ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 **ТЕКУЩИЕ ПАРАМЕТРЫ:**\n"
        f"• Стоимость рассылки: {CONFIG['BROADCAST_COST']} RUB\n"
        f"• Награда за чат: {CONFIG['REWARD_PUBLIC']} RUB\n"
        f"• Награда за папку: {CONFIG['REWARD_ADDLIST']} RUB\n"
        f"• Макс. аккаунтов: {CONFIG['MAX_ACCOUNTS']}\n"
        f"• Макс. чатов: {CONFIG['MAX_CHATS']}\n\n"
        "*Для изменения настроек обратитесь к разработчику.*"
    )
    
    await message.answer(settings_text)

@router.message(F.text == "🔙 В меню")
async def back_to_menu(message: types.Message):
    async with async_session() as session:
        user = (await session.execute(select(User).where(User.user_id == message.from_user.id))).scalar_one_or_none()
        is_admin = user.is_admin if user else False
    
    await message.answer("👇 **Выберите действие в меню:**", reply_markup=get_main_kb(user))

@router.message(F.text == "ℹ️ Информация")
async def show_info(message: types.Message):
    info_text = (
        "ℹ️ **ИНФОРМАЦИЯ О БОТЕ**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 **Marketing Engine Bot v1.1**\n\n"
        "🚀 **ОСНОВНЫЕ ФУНКЦИИ:**\n"
        "• Управление аккаунтами Telegram\n"
        "• Добавление чатов и папок\n"
        "• Массовая рассылка сообщений\n"
        "• Пополнение баланса\n"
        "• Награды за добавление контента\n\n"
        "💰 **ТАРИФЫ:**\n"
        f"• Рассылка: {CONFIG['BROADCAST_COST']} RUB\n"
        f"• Награда за чат: {CONFIG['REWARD_PUBLIC']} RUB\n"
        f"• Награда за папку: {CONFIG['REWARD_ADDLIST']} RUB\n\n"
        "📞 **ПОДДЕРЖКА:**\n"
        "По вопросам работы бота обращайтесь к администратору."
    )
    
    await message.answer(info_text, parse_mode="Markdown")

# Обработка других callback-запросов
@router.callback_query()
async def handle_all_callbacks(callback: types.CallbackQuery):
    # Обработка всех callback-запросов
    await callback.answer("Команда обработана")

# --- ЗАПУСК ---

async def main():
    await init_db()
    
    # Проверяем токен перед запуском
    try:
        me = await bot.get_me()
        print(f"✅ Бот авторизован: @{me.username} (ID: {me.id})")
        print(f"📊 Версия: Marketing Engine Bot v1.1")
        print(f"💎 Стоимость рассылки: {CONFIG['BROADCAST_COST']} RUB")
        print(f"📁 Папки: {'✅ Доступны' if CHATLISTS_AVAILABLE else '❌ Недоступны'}")
    except Exception as e:
        logger.error(f"❌ ОШИБКА АВТОРИЗАЦИИ БОТА: {e}")
        print("\n" + "="*50)
        print("!!! ОШИБКА: НЕВЕРНЫЙ ТОКЕН БОТА !!!")
        print("Пожалуйста, откройте main.py и замените значение CONFIG['BOT_TOKEN']")
        print("на актуальный токен от @BotFather.")
        print("="*50 + "\n")
        return

    # Убираем старые webhook если были
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logger.warning(f"Ошибка при удалении вебхука (не критично): {e}")
    
    print("🤖 Бот v1.1 запущен! Нажмите Ctrl+C для выхода.")
    print("✨ Особенности версии 1.1:")
    print("   • Новая база данных")
    print("   • Система пополнения баланса")
    print("   • Расширенная админ-панель")
    print("   • Подтверждение рассылок")
    print("   • Баннер приветствия")
    print(f"   • Папки: {'✅ Доступны' if CHATLISTS_AVAILABLE else '❌ Недоступны'}")
    try:
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        logger.error(f"Critical error: {e}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен.")
    except Exception as e:
        logger.error(f"Fatal error: {e}\n{traceback.format_exc()}")
